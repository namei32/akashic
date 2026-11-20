"""Start exactly the proposed plugin graph in a disposable WSL service."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import suppress
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from typing import Any, cast
from uuid import uuid4

from agent.plugins.artifacts import relative_artifact_pointer, write_pointers
from agent.plugins.bundles import set_plugin_choice
from agent.plugins.distribution_sources import distribution_sources
from agent.plugins.install import install_git_plugin
from agent.plugins.input_preparation import SOURCE_EXCLUDED_NAMES, _source_revision
from agent.plugins.python_environment import PythonEnvironments, read_environment_refs, ENVIRONMENT_FILE, OfflineWheels
from agent.plugins.selection import PluginSelection
from agent.plugins.source_resolver import ResolvedPluginSource
from agent.plugins.static_manifest import load_static_plugin_manifest
from agent.migrations.runner import initialize_empty_workspace
from scripts.install_plugin_distribution import _prepare_distribution_inputs

ROOT = Path(__file__).resolve().parents[1]


def failed_plugins(status: Mapping[str, Any]) -> list[dict[str, str]]:
    return [{"plugin": str(row["plugin_id"]), "error": str(row.get("load_error") or row.get("state"))[:350]}
            for row in status["plugins"] if row.get("enabled") is True and row.get("selected_ref")
            and (row.get("state") != "active" or row.get("input_ref") != row.get("selected_ref"))]


def start_probe(staged: Path, dist: Path, live: Path, plugins_home: Path,
                candidate: dict[str, ResolvedPluginSource], targets: list[dict[str, Any]]) -> dict[str, Any]:
    probe = staged / "boot-probe"
    workspace = probe / "workspace"
    workspace.mkdir(parents=True)
    replacements = {target["plugin_id"]: target for target in targets}
    sources: list[dict[str, Any]] = []
    environments = PythonEnvironments(workspace)
    for identity, source in candidate.items():
        code = source.plugin_root
        row: dict[str, Any] = {"id": identity, "code": str(code), "source": source.source_type,
                               "distribution": source.distribution_source, "wheels": source.wheel_tree_sha256}
        if identity in replacements:
            target = replacements[identity]
            wheels: OfflineWheels | None = target["wheels"]
            row["replacement"] = {"bundle": str(target["bundle"]), "commit": target["commit"],
                                  "wheels": [str(wheels.directory), wheels.tree_sha256] if wheels else None}
        elif not source.distribution_source:
            live_code = code
            plugin_cache = probe / "plugins" / "cache" / source.marketplace / source.plugin_name
            code = plugin_cache / ".artifacts" / live_code.name
            revision = _source_revision(live_code)
            shutil.copytree(live_code, code, symlinks=True,
                            ignore=shutil.ignore_patterns(*(SOURCE_EXCLUDED_NAMES - {ENVIRONMENT_FILE, ".akashic-source.json"})))
            if _source_revision(code) != revision:
                raise RuntimeError(f"隔离制品与正式保留源码不一致: {identity}")
            pointer = relative_artifact_pointer(plugin_cache, code)
            write_pointers(plugin_cache, stable=pointer, latest=pointer)
            manifest = load_static_plugin_manifest(code)
            if manifest.python:
                for ref in read_environment_refs(code, manifest).values():
                    destination = environments.path / ref
                    if not destination.exists():
                        shutil.copytree(PythonEnvironments(live).path / ref, destination, symlinks=True)
            row["code"] = str(code)
        sources.append(row)
    spec = probe / "sources.json"
    spec.write_text(json.dumps(sources), encoding="utf-8")
    (probe / "home").mkdir()
    alias_source = probe / "socket-aliases"
    alias_source.mkdir(mode=0o700)
    alias_target = Path(f"/tmp/akashic-web-{os.getuid()}")
    inaccessible = {live, live.parent, plugins_home, Path(f"/run/user/{os.getuid()}"), Path("/run/WSL"), Path("/mnt")}
    inaccessible.update(Path.home() / name for name in (".config", ".ssh", ".aws", ".codex", ".local/share/gh-config"))
    if any(probe.is_relative_to(path) for path in inaccessible):
        raise RuntimeError("隔离预检目录与正式实例重叠")
    result = subprocess.run([
        "systemd-run", "--user", "--wait", "--pipe", "--collect", f"--unit=akashic-boot-probe-{uuid4().hex}",
        f"--working-directory={ROOT}", "-p", "PrivateUsers=yes", "-p", "PrivateNetwork=yes",
        "-p", "PrivateDevices=yes", "-p", "NoNewPrivileges=yes", "-p", "ProtectSystem=strict",
        "-p", "KillMode=control-group", "-p", "RuntimeMaxSec=150", "-p", "TimeoutStopSec=10",
        "-p", f"BindPaths={alias_source}:{alias_target}", "-p", f"ReadWritePaths={probe} {alias_target}",
        "-p", "InaccessiblePaths=" + " ".join(f"-{path}" for path in sorted(inaccessible)),
        "/usr/bin/env", "-i", "PATH=/usr/local/bin:/usr/bin:/bin", f"HOME={probe / 'home'}", f"TMPDIR={probe}",
        "PYTHONDONTWRITEBYTECODE=1", sys.executable, "-B", "-m", "scripts.release_boot_probe", str(spec), str(dist),
    ], env={**os.environ, "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"}, capture_output=True, text=True, timeout=170)
    evidence_path = probe / "result.json"
    if not evidence_path.exists():
        raise RuntimeError(f"隔离启动预检未生成回执（退出 {result.returncode}）: {result.stderr[-450:].strip()}")
    evidence = json.loads(evidence_path.read_text())
    if result.returncode or evidence.get("state") != "ready":
        diagnostics = [line for line in result.stderr.splitlines() if "[web_shell" in line or "Permission denied" in line]
        if diagnostics:
            print("\n".join(diagnostics[-12:]), file=sys.stderr)
        raise RuntimeError(f"隔离启动预检失败: {json.dumps(evidence, ensure_ascii=False)[:1600]}")
    return evidence


def prepare(spec: Path, dist: Path) -> tuple[Path, Path, dict[str, ResolvedPluginSource]]:
    workspace = spec.parent / "workspace"
    home = spec.parent / "plugins"
    config = spec.parent / "config.toml"
    config.write_text("")
    home.mkdir(exist_ok=True)
    os.environ.update(AKASHIC_PLUGIN_HOME=str(home), AKASHIC_PLUGIN_DISTRIBUTION=str(dist))
    selection = PluginSelection(workspace)
    initialize_empty_workspace(repo_root=ROOT, workspace=workspace, config_path=config)
    selection.initialize()
    available = distribution_sources(workspace, home, dist)
    candidate = {}
    for row in json.loads(spec.read_text()):
        name, marketplace = row["id"].rsplit("@", 1)
        set_plugin_choice(workspace, row["id"], enabled=True)
        code = Path(row["code"])
        replacement = row.get("replacement")
        if replacement is not None:
            wheels = replacement["wheels"]
            installed = install_git_plugin(workspace=workspace, plugins_home=home, marketplace=marketplace,
                                           source=replacement["bundle"], ref_name=replacement["commit"],
                                           offline_wheels=OfflineWheels(Path(wheels[0]), wheels[1]) if wheels else None)
            code = installed.installed_path
        candidate[row["id"]] = ResolvedPluginSource(code, row["source"], marketplace, name, load_static_plugin_manifest(code),
                                                   wheel_tree_sha256=row["wheels"], distribution_source=row["distribution"])
    components = _prepare_distribution_inputs(available=available, candidate=candidate, selected={}, distribution=dist,
                                              workspace=workspace, plugins_home=home, selection=selection)
    selection.commit(components, expected_ref=None)
    return workspace, config, candidate


async def probe_boot(spec: Path, dist: Path) -> dict[str, Any]:
    from agent.config_models import Config
    from bootstrap.app import build_app_runtime
    from bootstrap.runtime_readiness import RuntimeReadiness

    workspace, config, candidate = prepare(spec, dist)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    os.environ.update(AKASHIC_WEB_PORT=str(port), AKASHIC_WEB_HOST="127.0.0.1")
    readiness = RuntimeReadiness(workspace, uuid4().hex)
    runtime = build_app_runtime(Config.load(config, workspace=workspace), workspace, readiness=readiness)
    running = asyncio.create_task(runtime.run())
    try:
        async with asyncio.timeout(90):
            while not readiness.ready:
                if running.done():
                    await running
                    raise RuntimeError("隔离 Core 在就绪前退出")
                await asyncio.sleep(0.1)
        if runtime.core is None:
            raise RuntimeError("隔离 Core 未启动")
        status = cast(dict[str, Any], runtime.core.plugin_manager.plugin_status())
        missing = failed_plugins(status)
        active = {row["plugin_id"] for row in status["plugins"] if row.get("state") == "active"}
        if missing or set(candidate) != active:
            return {"state": "failed", "plugins": missing, "missing": sorted(set(candidate) - active),
                    "unexpected": sorted(active - set(candidate))}
        await conversation_probe(workspace, port)
        return {"state": "ready", "plugins": len(candidate), "conversation": "passed"}
    finally:
        if not running.done():
            running.cancel()
        with suppress(asyncio.CancelledError):
            await running


async def conversation_probe(workspace: Path, port: int) -> None:
    from aiohttp import web
    from websockets.asyncio.client import connect

    sys.path.insert(0, str(ROOT / "sdk/python/src"))
    from akashic_sdk import AsyncAkashic
    from plugins.gateway.cli import find_endpoint, read_token

    async def models(_request: web.Request) -> web.Response:
        return web.json_response({"data": [{"id": "fixture", "object": "model"}]})

    async def chat(request: web.Request) -> web.StreamResponse:
        if not (await request.json()).get("stream"):
            return web.json_response({"id": "fixture", "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": "release-probe-ready"}}]})
        stream = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await stream.prepare(request)
        data = {"choices": [{"index": 0, "delta": {"content": "release-probe-ready"}, "finish_reason": "stop"}]}
        await stream.write(f"data: {json.dumps(data)}\n\ndata: [DONE]\n\n".encode())
        await stream.write_eof()
        return stream

    app = web.Application()
    app.router.add_get("/models", models)
    app.router.add_post("/chat/completions", chat)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        model_port = listener.getsockname()[1]
        await web.SockSite(runner, listener).start()
        endpoint = find_endpoint(workspace)
        async with await AsyncAkashic.connect(endpoint, workspace_token=read_token(workspace, endpoint)) as client:
            result = await client.request("models/command", {"type": "create_connection_with_model",
                "connection": {"expected_revision": 0, "connection_id": "fixture", "name": "Probe",
                    "driver_id": "openai-compatible", "endpoint": f"http://127.0.0.1:{model_port}",
                    "auth_identity": "fixture", "credential": {"api_key": "fixture"}},
                "model": {"expected_revision": 0, "model_id": "fixture", "connection_id": "fixture",
                    "kind": "chat", "model": "fixture", "capabilities": {"context_window": 32000,
                    "max_output_tokens": 4096, "supports_tool_calls": True}, "capability_sources": {}}})
            if not isinstance(result, dict) or result["status"] != 200:
                raise RuntimeError("隔离模型接入失败")
            await client.request("models/command", {"type": "set_default", "expected_revision": result["body"]["revision"],
                                                    "role": "default", "model_id": "fixture"})
            session = "akashic:" + uuid4().hex
            secret = workspace / "web-auth/secret"
            headers = {"Authorization": "Bearer " + secret.read_text().strip()} if secret.exists() else {}
            async with connect(f"ws://127.0.0.1:{port}/ws", additional_headers=headers, proxy=None) as web_chat:
                await web_chat.send(json.dumps({"type": "message.send", "request_id": "release-probe-input",
                                                "session_id": session, "text": "Reply to the release probe."}))
                await web_chat.send(json.dumps({"type": "session.follow", "request_id": "release-probe-follow",
                                                "session_id": session, "version": 2, "after_seq": -1}))
                async with asyncio.timeout(15):
                    async for raw in web_chat:
                        frame = json.loads(raw)
                        if frame.get("session_id") != session:
                            continue
                        if frame["type"] == "messages.appended" and any(
                            item["body"].get("finish") == "complete" and "release-probe-ready" in str(item["body"].get("parts"))
                            for item in frame["items"]):
                            return
            raise RuntimeError("隔离聊天缺少最终回复")
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    spec, distribution = map(Path, sys.argv[1:])
    try:
        verdict = asyncio.run(probe_boot(spec, distribution))
    except Exception as error:
        verdict = {"state": "failed", "error": f"{type(error).__name__}: {error}"[:600]}
    (spec.parent / "result.json").write_text(json.dumps(verdict, ensure_ascii=False), encoding="utf-8")
    sys.exit(0 if verdict["state"] == "ready" else 1)
