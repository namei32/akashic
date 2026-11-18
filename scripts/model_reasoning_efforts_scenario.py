"""Exercise the Models owner RPC in a temporary workspace without provider traffic."""
from __future__ import annotations

import asyncio
import tempfile
from dataclasses import replace
from pathlib import Path

from plugins.models.contract import ChatModelSelection, ModelRequest
from plugins.models.model_settings_http import rpc_methods
from plugins.models.settings import AddModel, SetDefaultModel
from tests.support.material_models import material_models


async def scenario(workspace: Path) -> None:
    async with material_models(workspace) as models:
        class Control:
            async def catalog(self):
                return models.state.catalog_snapshot()

            async def apply(self, command):
                return await models.settings.apply(command)

        rpc = rpc_methods(Control())

        async def call(name, payload):
            method = rpc[f"models/{name}"]
            return await method.call(method.params.model_validate(payload))

        initial = models.store.read_snapshot()
        original = initial.models[initial.role_bindings["default"]]
        await models.settings.apply(AddModel(
            expected_revision=initial.revision, model_id="reasoning-fixture",
            connection_id=original.connection_id, kind="chat", model="fixture-reasoning",
            capabilities=replace(original.capabilities, supported_reasoning_efforts=("medium",)),
            capability_sources=original.capability_sources, default_reasoning_effort="medium",
            driver_config={"reasoning_summary": "none"},
        ))
        revision = models.store.read_snapshot().revision
        await models.settings.apply(SetDefaultModel(revision, "default", "reasoning-fixture"))
        before = models.store.read_snapshot()
        old_model = before.models["reasoning-fixture"]
        payload = {"type": "extend_model_reasoning_efforts", "expected_revision": before.revision,
                   "model_id": old_model.model_id, "efforts": ["low", "high", "xhigh"]}
        expected = ("medium", "low", "high", "xhigh")
        async with models.chat_models.execution() as pinned:
            assert pinned.chat("agent").descriptor.reasoning_effort == "medium"
            result = await call("command", payload)
            assert result["status"] == 200 and result["body"]["revision"] == before.revision + 1, result
            assert pinned.chat("agent").descriptor.capabilities.supported_reasoning_efforts == ("medium",)
        after = models.store.read_snapshot()
        updated = replace(old_model,
            capabilities=replace(old_model.capabilities, supported_reasoning_efforts=expected),
            capability_sources=replace(old_model.capability_sources, reasoning_efforts="user"),
        )
        assert after == replace(before, revision=before.revision + 1,
                                models={**before.models, old_model.model_id: updated})
        catalog = await call("catalog", {})
        assert catalog["status"] == 200
        entry = next(item for item in catalog["body"]["models"] if item["id"] == old_model.model_id)
        assert entry["defaultReasoningEffort"] == "medium"
        assert entry["capabilities"]["supportedReasoningEfforts"] == list(expected)
        assert (await call("command", payload))["status"] == 409
        payload["expected_revision"] = after.revision
        assert (await call("command", payload))["body"]["revision"] == after.revision
        assert (await call("command", {**payload, "efforts": [" high "]}))["status"] == 422
        assert (await call("command", {**payload, "model_id": after.default_embedding_model_id}))["status"] == 422
        assert models.store.read_snapshot() == after
        async with models.chat_models.execution() as execution:
            assert execution.chat("agent").descriptor.reasoning_effort == "medium"
        for effort in expected[1:]:
            selected = ChatModelSelection(old_model.model_id, effort)
            assert models.state.validate_chat_selection(selected) == selected
            async with models.chat_models.execution(model_id=selected.model_id, reasoning_effort=effort) as execution:
                response = await execution.chat("agent").complete(
                    ModelRequest(messages=({"role": "user", "content": "Reply OK."},)))
                assert response.call_record_id
            assert models.driver.chat_bindings[-1].reasoning_effort == effort
        models.store.integrity_check()
        print("PASS RPC append, unchanged bindings/default, pinned session, CAS, idempotency, validation, catalog and fixture driver")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="model-reasoning-efforts-") as directory:
        asyncio.run(scenario(Path(directory)))
