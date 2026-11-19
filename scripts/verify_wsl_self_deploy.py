"""Verify native publishing with real systemd, Gateway, installer and temporary data."""
from __future__ import annotations

import asyncio
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from typing import Any, cast
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "sdk/python/src")]

from akashic_sdk import AsyncAkashic
from agent.plugins.bundles import set_plugin_choice
from plugins.gateway.cli import find_endpoint, read_token
from scripts.akashic_release.manifest import read_json, write_json
from scripts.akashic_release.wsl import Host, sha256
from scripts.build_plugin_distribution import build
from scripts.install_plugin_distribution import ensure_bundle, extract_core
from scripts.verify_self_deploy_runtime import configure_model, start_model_server


def run(*argv: str, cwd: Path | None = None) -> str:
    return subprocess.check_output(argv, cwd=cwd, text=True, timeout=300).strip()


def commit(repo: Path, message: str) -> str:
    run("git", "add", ".", cwd=repo)
    run("git", "-c", "user.name=Scenario", "-c", "user.email=scenario@example.invalid",
        "-c", "commit.gpgSign=false", "commit", "-qm", message, cwd=repo)
    return run("git", "rev-parse", "HEAD", cwd=repo)


def releases(folder: Path, wheels: Path) -> list[tuple[str, Path, Path]]:
    repo = folder / "source"
    run("git", "clone", "--shared", "--quiet", str(ROOT), str(repo))
    run("git", "checkout", "--quiet", "--detach", run("git", "-C", str(ROOT), "rev-parse", "HEAD"), cwd=repo)
    for name in ("scripts/akashic_release/cli.py", "scripts/akashic_release/self_deploy.py",
                 "scripts/akashic_release/wsl.py", "plugins/standard_tools/skills/deploy-akashic/SKILL.md"):
        shutil.copyfile(ROOT / name, repo / name)
    commits = [commit(repo, "Isolated bootstrap")]
    jobs = repo / "plugins/jobs/plugin.py"
    text = jobs.read_text()
    for version in ("9.0.2", "9.0.3"):
        jobs.write_text(re.sub(r'^version = .+$', f'version = "{version}"', text, flags=re.M))
        commits.append(commit(repo, f"Jobs {version}"))

    def package(sha: str) -> tuple[str, Path, Path]:
        destination = folder / sha
        dist = destination / "distribution"
        destination.mkdir()
        build(repo, sha, dist)
        shutil.copytree(wheels, dist / "wheels")
        return sha, dist, extract_core(dist, destination / "core")

    with ThreadPoolExecutor(max_workers=3) as executor:
        return list(executor.map(package, commits))


def external(folder: Path) -> list[tuple[str, Path]]:
    source = folder / "external"
    source.mkdir()
    run("git", "init", "--quiet", "--initial-branch=main", cwd=source)
    result = []
    for version in ("1.0.0", "2.0.0"):
        (source / "plugin.py").write_text(
            f'api_version = 3\nname = "release_probe"\nversion = "{version}"\n'
            'async def apply(ctx):\n    pass\n'
        )
        sha = commit(source, version)
        bundle = folder / f"probe-{version}.bundle"
        run("git", "bundle", "create", str(bundle), "HEAD", "main", cwd=source)
        result.append((sha, bundle))
    return result


async def connect(host: Host) -> AsyncAkashic:
    endpoint = find_endpoint(host.workspace)
    return await AsyncAkashic.connect(endpoint, workspace_token=read_token(host.workspace, endpoint))


async def wait_job(path: Path, *, status: str, require_error: bool = False) -> dict[str, Any]:
    async with asyncio.timeout(150):
        while True:
            if path.exists():
                record = read_json(path)
                if record["status"] == status and (not require_error or record.get("error")):
                    return record
                if record["status"] == "failed_preflight" or record.get("error") and status == "active":
                    raise RuntimeError(record.get("error", record["status"]))
            await asyncio.sleep(0.2)


async def submit(host: Host, folder: Path, subcommand: list[str], session: str,
                 *, status: str = "active") -> tuple[Path, dict[str, Any]]:
    (folder / "next.json").write_text(json.dumps(subcommand))
    async with await connect(host) as client:
        await client.request("programmatic/session/admit", {"session_id": session, "persist_memory": False})
        async with await client.session_follow(session) as feed:
            await client.request("programmatic/message/send", {"session_id": session, "message_id": session + ":input",
                                                               "text": "Use deploy-akashic and finish normally."})
            async with asyncio.timeout(45):
                async for _ in feed.events():
                    page = await client.message_read(session, limit=100)
                    if any(item["body"].get("kind") == "output" and item["body"].get("finish") == "complete"
                           for item in page["items"]):
                        break
            record = (host.root / "run/self-deploy").glob("*.json")
            matches = [path for path in record if read_json(path)["request"]["stop"]["session_id"] == session]
            assert len(matches) == 1
            path = matches[0]
            return path, await wait_job(path, status=status, require_error=status == "selected_not_started")


async def plugins(host: Host) -> dict[str, Any]:
    async with await connect(host) as client:
        status = cast(dict[str, Any], await client.request("plugin/status", {}))
        return {row["plugin_id"]: row for row in status["plugins"] if row.get("state") == "active"}


async def scenario(folder: Path, compiled: list[tuple[str, Path, Path]], probe: list[tuple[str, Path]]) -> None:
    state, root, home = folder / "state", folder / "releases", folder / "home"
    for path in (state, root, home):
        path.mkdir()
    os.environ["HOME"] = str(home)
    unit = f"akashic-wsl-{uuid4().hex[:12]}.service"
    profile = folder / "host.json"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    write_json(profile, {"root": str(root), "state": str(state), "unit": unit, "port": port})
    host = Host(profile)
    (folder / "shell.py").write_text(
        "import json,os,sys\nfrom pathlib import Path\n"
        f"args=json.loads(Path({str(folder / 'next.json')!r}).read_text())\n"
        f"os.execv(sys.executable,[sys.executable,'-B',{str(ROOT / 'scripts/akashic_release/cli.py')!r},"
        f"'wsl','--profile',{str(profile)!r},*args])\n"
    )
    pythonpath = f"{ROOT}:{ROOT / 'sdk/python/src'}"
    # Volatile, uniquely named unit. Failure injection applies only to this instance.
    unit_root = Path(f"/run/user/{os.getuid()}/systemd/user")
    unit_root.mkdir(parents=True, exist_ok=True)
    unit_file = unit_root / unit
    hold = folder / "hold-start"
    unit_text = f"""[Unit]
Description=Isolated Akashic native deployment test
[Service]
Type=exec
KillMode=mixed
WorkingDirectory={ROOT}
Environment=HOME={home}
Environment=PYTHONPATH={pythonpath}
ExecStartPre=/usr/bin/test ! -e {hold}
ExecStart={sys.executable} -B -m scripts.akashic_release.wsl --profile {profile} launch
"""
    base, base_dist, base_core = compiled[0]
    run(sys.executable, "-B", str(base_core / "main.py"), "init", "--config", str(state / "config.toml"),
        "--workspace", str(host.workspace))
    ensure_bundle(base_dist, base_dist / "bundles/base.toml", workspace=host.workspace, plugins_home=state / "plugin-home",
                  config_path=state / "config.toml", receipt_path=host.workspace / "runtime/distribution-install.json")
    for _, dist, core in compiled:
        run(sys.executable, "-B", str(ROOT / "scripts/akashic_release/cli.py"), "wsl", "--profile", str(profile),
            "register", "--core", str(core), "--distribution", str(dist), "--python", sys.executable)
    # Computer needs a separate workload controller. This scenario uses real Web/API and Shell.
    if any(row["name"] == "computer" for row in read_json(base_dist / "distribution.json")["plugins"]):
        set_plugin_choice(host.workspace, "computer@release", enabled=False)
        host.publish(base, _plan(host, folder / "initial.json", []), base_dist)
    write_json(host.launch_path, {"commit": base})
    cli = [sys.executable, "-B", str(ROOT / "scripts/akashic_release/cli.py"), "wsl", "--profile", str(profile)]
    server, model_port = await start_model_server(folder, command=f"{sys.executable} -B {folder / 'shell.py'}")
    try:
        with unit_file.open("x") as stream:
            stream.write(unit_text)
        run("systemctl", "--user", "daemon-reload")
        initial = host.selection.read()
        assert initial is not None
        first = await asyncio.to_thread(host.activate, base, initial)
        async with await connect(host) as client:
            await configure_model(client, model_port)
        sha, bundle = probe[0]
        installed = json.loads(await asyncio.to_thread(run, *cli, "plugin-install", "--source", str(bundle), "--ref", sha))
        async with asyncio.timeout(30):
            while True:
                hot = json.loads(await asyncio.to_thread(run, *cli, "plugin-status", installed["update_id"]))
                if hot["state"] == "active" and hot["active_input_ref"] == hot["input_ref"]:
                    break
                await asyncio.sleep(0.2)
        assert host.ready()["bootId"] == first["bootId"]
        hot_input = (await plugins(host))["release_probe@local"]["input_ref"]
        print("HOT active without restarting", flush=True)

        builtin, _, _ = compiled[1]
        wrong = folder / "stale-plan.json"
        old = host.ready()
        write_json(wrong, {"schema_version": 1, "expected_root_ref": "0" * 64, "targets": []})
        await submit(host, folder, ["submit", "--commit", builtin, "--plan", str(wrong)],
                     "programmatic:wsl-stale", status="failed_preflight")
        assert host.ready() == old
        print("PREFLIGHT refused a stale Root without restarting", flush=True)
        builtin_session = "programmatic:wsl-builtin"
        _, before = await submit(host, folder, ["submit", "--commit", builtin], builtin_session)
        snapshot = await plugins(host)
        assert snapshot["jobs@release"]["version"] == "9.0.2"
        assert snapshot["release_probe@local"]["input_ref"] == hot_input
        assert before["ready"]["bootId"] != first["bootId"]
        async with await connect(host) as client:
            history = (await client.message_read(builtin_session, limit=100))["items"]
        assert any(item["id"] == builtin_session + ":input" for item in history)
        print("BUILTIN active and external input preserved", flush=True)

        mixed, _, _ = compiled[2]
        sha, bundle = probe[1]
        target = {"plugin_id": "release_probe@local", "bundle_relative_path": bundle.name,
                  "bundle_sha256": sha256(bundle), "target_commit": sha}
        plan = _plan(host, folder / "mixed.json", [target])
        hold.touch()
        job, failed = await submit(host, folder, ["submit", "--commit", mixed, "--plan", str(plan), "--inputs", str(folder)],
                                   "programmatic:wsl-mixed", status="selected_not_started")
        assert host.selection.read() == failed["publication"]["new_root_ref"]
        # Recreate the durable launch state immediately after publication and before activation.
        write_json(host.launch_path, {"commit": builtin, "blocked": job.stem})
        hold.unlink()
        await asyncio.to_thread(run, *cli, "resume", job.stem)
        after = await plugins(host)
        assert after["jobs@release"]["version"] == "9.0.3" and after["release_probe@local"]["version"] == "2.0.0"
        assert host.selection.read() == failed["publication"]["new_root_ref"]
        print("MIXED failed startup resumed without republishing", flush=True)

        await asyncio.to_thread(run, *cli, "rollback", job.stem, "--compatible-data")
        restored = await plugins(host)
        assert restored["jobs@release"]["version"] == "9.0.2" and restored["release_probe@local"]["version"] == "1.0.0"
        assert host.ready()["sourceCommit"] == builtin
        async with await connect(host) as client:
            original = await client.message_read(builtin_session, limit=100)
            assert original["items"] == history
        print("ROLLBACK restored both inputs and retained the complete original conversation", flush=True)

        _plan(host, plan, [target])
        hold.touch()
        job, _ = await submit(host, folder, ["submit", "--commit", mixed, "--plan", str(plan), "--inputs", str(folder)],
                              "programmatic:wsl-rollback", status="selected_not_started")
        hold.unlink()
        await asyncio.to_thread(run, *cli, "rollback", job.stem, "--compatible-data")
        restored = await plugins(host)
        assert restored["jobs@release"]["version"] == "9.0.2" and restored["release_probe@local"]["version"] == "1.0.0"
        assert host.ready()["sourceCommit"] == builtin
        requests = json.loads((folder / "requests.json").read_text())
        assert any("deploy-akashic" in str(message.get("content")) and "原生 WSL" in str(message.get("content"))
                   for request in requests for message in request["messages"] if message["role"] in {"system", "developer"})
        print("ROLLBACK recovered directly from a failed startup", flush=True)
    finally:
        await server.cleanup()
        if unit_file.exists():
            try:
                run("systemctl", "--user", "stop", unit)
            finally:
                unit_file.unlink()
                run("systemctl", "--user", "daemon-reload")


def _plan(host: Host, path: Path, targets: list[dict[str, Any]]) -> Path:
    write_json(path, {"schema_version": 1, "expected_root_ref": host.selection.read(), "targets": targets})
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheels", type=Path)
    parser.add_argument("--reuse", type=Path, help="Reuse built fixture releases in an earlier evidence directory")
    args = parser.parse_args()
    folder = Path(tempfile.mkdtemp(prefix="ak-wsl-"))
    print("EVIDENCE", folder, flush=True)
    if args.reuse:
        fixture = args.reuse.resolve(strict=True)
        commits = run("git", "-C", str(fixture / "source"), "log", "-3", "--reverse", "--format=%H").splitlines()
        compiled = [(sha, fixture / sha / "distribution", fixture / sha / "core") for sha in commits]
    else:
        compiled = releases(folder, args.wheels.resolve(strict=True))
    asyncio.run(scenario(folder, compiled, external(folder)))
