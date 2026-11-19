"""Chromium -> real Wake dashboard/config generation -> real Drift source capture.

Disposable workspace and local model/receiver only; no account or production data.
The tiny browser host only mounts the built plugin and rebinds request identities.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile

from fastapi import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from active_sources_scenario import TARGET, configure as active_configure
from default_memory_scenario import run
from drift_memory_wake_scenario import wait_for_tool_clock


def configure(sources: Path, workspace: Path) -> None:
    from agent.plugin_composition.config_input import save_config
    active_configure(sources, workspace)
    shutil.copytree(ROOT / "plugins/ui", sources / "ui", ignore=shutil.ignore_patterns("__pycache__"))
    save_config(workspace / "plugin-data/wake-builtin", {"enabled": True, "delivery": TARGET, "timezone": "UTC"})


async def scenario(output: Path, chromium: str) -> dict[str, object]:
    import httpx
    import uvicorn
    from fastapi import FastAPI
    from fastapi.responses import FileResponse, HTMLResponse, Response
    from playwright.async_api import async_playwright
    from agent.plugin_composition.config_input import load_config
    from core.common.unix_socket import socket_alias_path
    from plugins.drift.contract import DRIFT_CHANGED
    from plugins.drift.store import DriftStore
    from plugins.ledger.contract import MESSAGE_CATALOG
    from plugins.ui.contract import UI, WEB_UI
    from plugins.wake.request import read_request

    entered = asyncio.Event()
    prompts: list[str] = []

    async def chat(payload):
        if "skip_content" not in json.dumps(payload.get("tools", [])):
            return None
        prompts.append(json.dumps(payload, ensure_ascii=False))
        entered.set()
        return {"id": "ui-fixture", "object": "chat.completion", "model": "memory-fixture",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": None,
                "tool_calls": [{"id": "skip", "type": "function", "function": {
                    "name": "skip_content", "arguments": '{"reason":"fixture: no send"}'}}]}, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}

    async def exercise(host, append, workspace, requests):
        await append(TARGET["session_id"], "target", "Target conversation.")
        await append("akashic:active", "source", "ACTIVE_SOURCE_COFFEE")
        await append("akashic:excluded", "excluded", "EXCLUDED_SECRET")
        catalog = host.live_root.context.require(MESSAGE_CATALOG)
        before = {sid: catalog.reader(sid).snapshot() for sid in (TARGET["session_id"], "akashic:active", "akashic:excluded")}
        folder = workspace.parent
        entry = folder / "entry.jsx"
        entry.write_text('''import {activate} from PATH;
const entries = [];
const ctx = {http: {request: (url, init) => fetch(url, init)},
  ui: {inject: (_, apply) => apply({register: item => {entries.push(item); return () => {}}})}};
activate(ctx);
entries.find(item => item.id === 'wake-settings').render(document.getElementById('root'));
'''.replace("PATH", json.dumps(str(ROOT / "plugins/wake/web_module.js"))))
        subprocess.run([str(ROOT / "node_modules/.bin/esbuild"), str(entry), "--bundle", "--format=esm",
                        "--outfile=" + str(folder / "browser.js"), "--define:process.env.NODE_ENV=\"production\""], check=True)
        # The production module ships its stylesheet as a sibling asset.
        shutil.copyfile(ROOT / "plugins/wake/web_module.css", folder / "browser.css")
        app = FastAPI()
        posts: list[dict] = []
        socket_path = socket_alias_path(workspace / "runtime", "dashboard.sock")
        transport = httpx.AsyncHTTPTransport(uds=str(socket_path))
        async with httpx.AsyncClient(transport=transport, base_url="http://isolated-ui") as client:
            @app.get("/")
            async def index():
                return HTMLResponse('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
                    '<meta name="viewport" content="width=device-width,initial-scale=1"><title>Wake 隔离验收</title>'
                    '<link rel="stylesheet" href="/browser.css"><style>@font-face{font-family:fixture;src:url(/font.woff2)}'
                    'body{margin:0;font-family:fixture,serif}</style><body>'
                    '<div id="root"></div><script type="module" src="/browser.js"></script></body></html>')

            @app.get("/font.woff2")
            async def font():
                return FileResponse(ROOT / "frontend/theme/assets/fonts/LXGWWenKaiGBScreen.woff2")

            @app.get("/browser.{extension}")
            async def asset(extension: str):
                assert extension in {"js", "css"}
                return FileResponse(folder / ("browser." + extension))

            @app.api_route("/api/dashboard/{path:path}", methods=["GET", "POST"])
            async def proxy(path: str, request: Request):
                # Real UI middleware and request leases, not direct Settings calls.
                root = host.live_root.context
                state = await root.require(WEB_UI).state()
                module = next((item for item in root.require(UI).catalog().modules if item.plugin_id == "wake"), None)
                if module is None:
                    return Response('{"detail":"Wake 配置正在换代"}', status_code=503, media_type="application/json")
                body = await request.body()
                if request.method == "POST":
                    posts.append(json.loads(body))
                response = await client.request(request.method, "/api/dashboard/" + path, content=body, headers={
                    "content-type": "application/json", "x-akashic-web-snapshot": str(state["snapshotId"]),
                    "x-akashic-web-catalog": str(state["catalogId"]), "x-akashic-web-module": module.plugin_id,
                    "x-akashic-web-generation": module.generation_id})
                return Response(response.content, status_code=response.status_code,
                                media_type="application/json")

            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.setblocking(False)
            server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
            serving = asyncio.create_task(server.serve(sockets=[listener]))
            try:
                async with asyncio.timeout(10):
                    while not server.started:
                        if serving.done():
                            await serving
                        await asyncio.sleep(.02)
                url = f"http://127.0.0.1:{listener.getsockname()[1]}"
                await wait_for_tool_clock()
                async with async_playwright() as playwright:
                    browser = await playwright.chromium.launch(executable_path=chromium, headless=True, args=["--no-sandbox"])
                    try:
                        page = await browser.new_page(viewport={"width": 1100, "height": 850})
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        await page.goto(url)
                        toggle = page.get_by_role("switch", name="按用户活跃度自动选择会话")
                        await toggle.wait_for()
                        await page.evaluate("document.fonts.ready")
                        assert not await toggle.is_checked()
                        await page.screenshot(path=str(output / "before.png"), full_page=True)
                        await toggle.check()
                        await page.get_by_text("来源范围与优先级", exact=True).click()
                        await page.get_by_label("活跃统计窗口（天）", exact=True).fill("5")
                        await page.get_by_label("每次最多取材会话数", exact=True).fill("1")
                        await page.get_by_label("akashic · ACTIVE_SOURCE_COFFEE的取材方式", exact=True).select_option("preferred")
                        await page.get_by_label("akashic · EXCLUDED_SECRET的取材方式", exact=True).select_option("excluded")
                        await page.get_by_role("button", name="保存配置", exact=True).click()
                        await page.get_by_text("配置已生效", exact=True).wait_for()
                        selected = load_config(workspace / "plugin-data/wake-builtin")[0]
                        policy = selected["active_sources"]
                        assert isinstance(policy, dict)
                        assert policy == {"enabled": True, "revision": 1, "excluded": ["akashic:excluded"],
                                          "preferred": ["akashic:active"], "window_days": 5, "max_sessions": 1}, policy
                        assert selected["delivery"] == TARGET
                        # Real opportunity now captures the source selected by this UI save.
                        store = DriftStore(workspace / "plugin-data/drift-builtin/drift.sqlite3")
                        now = datetime.now(UTC)
                        store.propose("ui-enabled", "1", {"kind": "conversation",
                            "valid_until": (now + timedelta(minutes=5)).isoformat()}, now, initiative_target=TARGET)
                        host.live_root.context.emit(DRIFT_CHANGED, None)
                        await asyncio.wait_for(entered.wait(), 25)
                        frozen = [read_request(catalog.reader(sid).snapshot()) for sid in catalog.snapshot_heads(prefix="wake:")]
                        assert frozen[-1].source_evidence[0].session_id == "akashic:active"
                        assert "ACTIVE_SOURCE_COFFEE" in prompts[-1] and "EXCLUDED_SECRET" not in prompts[-1]
                        await page.reload()
                        await toggle.wait_for()
                        assert await toggle.is_checked()
                        # Check the actual controls at mobile and desktop widths.
                        await page.get_by_text("来源范围与优先级", exact=True).click()
                        for width in (320, 390, 720, 1365):
                            await page.set_viewport_size({"width": width, "height": 914})
                            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), width
                            await page.screenshot(path=str(output / f"enabled-{width}.png"), full_page=True)
                        await toggle.uncheck()
                        await page.get_by_role("button", name="保存配置", exact=True).click()
                        await page.get_by_text("配置已生效", exact=True).wait_for()
                        disabled = load_config(workspace / "plugin-data/wake-builtin")[0]
                        assert disabled["active_sources"] == {**policy, "enabled": False, "revision": 2}
                        assert disabled["delivery"] == TARGET
                        await page.reload()
                        await toggle.wait_for()
                        assert not await toggle.is_checked()
                        entered.clear()
                        now = datetime.now(UTC)
                        store.propose("ui-disabled", "1", {"kind": "conversation",
                            "valid_until": (now + timedelta(minutes=5)).isoformat()}, now, initiative_target=TARGET)
                        host.live_root.context.emit(DRIFT_CHANGED, None)
                        await asyncio.wait_for(entered.wait(), 25)
                        disabled_requests = [read_request(catalog.reader(sid).snapshot())
                                             for sid in catalog.snapshot_heads(prefix="wake:")]
                        assert any(not item.source_policy and "Target conversation." in item.history
                                   and "ACTIVE_SOURCE_COFFEE" not in item.history for item in disabled_requests)
                        # The old revision may not re-enable the policy through HTTP.
                        current = host.read_config_input("wake")
                        rejected = await page.request.post(url + "/api/dashboard/wake/config", data={
                            "request_id": "stale-policy", "expected_input": current["input_ref"],
                            "values": {"enabled": True, "active_sources": policy}})
                        assert rejected.status == 422, await rejected.text()
                        assert "revision" in await rejected.text()
                        # A conflicting save is visible and retains the unsaved draft.
                        async def stale_save(route):
                            if route.request.method == "POST":
                                payload = route.request.post_data_json
                                payload["expected_input"] = "0" * 64
                                await route.continue_(post_data=json.dumps(payload))
                            else:
                                await route.continue_()
                        await page.route("**/api/dashboard/wake/config", stale_save)
                        await toggle.check()
                        await page.get_by_role("button", name="保存配置", exact=True).click()
                        await page.get_by_role("alert").get_by_text("配置已更新，请刷新后重试", exact=True).wait_for()
                        assert await toggle.is_checked()
                        assert load_config(workspace / "plugin-data/wake-builtin")[0]["active_sources"] == disabled["active_sources"]
                        for sid, rows in before.items():
                            assert catalog.reader(sid).snapshot() == rows
                        assert not errors, errors
                        assert len({post["request_id"] for post in posts[:2]}) == 2
                        return {"default_off": True, "browser_save_active_receipt": True,
                            "ui_policy_reaches_real_drift_capture": True, "reload_preserves_toggle": True,
                            "disable_increments_revision_preserves_options": True, "stale_revision_rejected": True,
                            "disabled_capture_returns_to_target": True, "conflict_visible_keeps_draft": True,
                            "delivery_target_unchanged": True, "original_messages_preserved": True,
                            "narrow_widths": [320, 390, 720, 1365], "browser_errors": errors,
                            "scope": "isolated real UI middleware, Settings and PluginManager; fixed local model, not chat-quality evidence"}
                    finally:
                        if "page" in locals():
                            (output / "last-dom.html").write_text(await page.content())
                            (output / "browser-errors.json").write_text(json.dumps(errors, ensure_ascii=False))
                            await page.screenshot(path=str(output / "last-page.png"), full_page=True)
                        await browser.close()
            finally:
                server.should_exit = True
                await serving
                listener.close()

    with tempfile.TemporaryDirectory(prefix="akashic-active-ui-") as temporary:
        return await run(Path(temporary), exercise, configure=configure, chat_handler=chat, setup_only=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--chromium", required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    report = asyncio.run(scenario(args.output, args.chromium))
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
