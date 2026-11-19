"""实际插件从窄生命周期端口结束 Core，验证失败与恢复。"""
from __future__ import annotations

import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PROVIDER = '''import asyncio
import os
import sys
from agent.plugin_composition.tasks import RESTART_GATE
api_version = 3
name = "exit_owner"
version = "1.0.0"
inject = (RESTART_GATE,)
async def apply(ctx):
    async def read_intent():
        reader = asyncio.StreamReader()
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
        try:
            if os.environ.get("AKASHIC_SUPERVISED") == "1":
                sys.stdout.write("x" * (1024 * 1024) + "\\n")
                sys.stderr.write("x" * (1024 * 1024) + "\\n")
                sys.stdout.flush()
                sys.stderr.flush()
            print("SCENARIO_READY", flush=True)
            line = await reader.readline()
            error = RuntimeError("native owner failed") if line == b"fail\\n" else None
            ctx.require(RESTART_GATE).request_shutdown(error)
            try:
                ctx.require(RESTART_GATE).check_open()
            except RuntimeError:
                print("ADMISSION_CLOSED", flush=True)
            else:
                raise AssertionError("停止后仍接纳新工作")
        except asyncio.CancelledError:
            cleanup_fd = os.environ.get("SCENARIO_CLEANUP_FD")
            if cleanup_fd is not None:
                os.write(int(cleanup_fd), b"1")
                await reader.readline()
            raise
        finally:
            transport.close()
    await ctx.spawn(read_intent(), name="native-intent")
'''


async def stop_ready_process(
    config: Path, workspace: Path, peer_workspace: Path, env: dict[str, str],
) -> None:
    """在真实插件清理期间重复送 SIGTERM，核对关闭回执。"""
    # 1. 使用宿主生命周期管道等待完整启动，避免按时间猜测就绪。
    read_fd, write_fd = os.pipe()
    reader = asyncio.StreamReader()
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(read_fd, "rb"))
    cleanup_read_fd, cleanup_write_fd = os.pipe()
    cleanup_reader = asyncio.StreamReader()
    cleanup_transport, _ = await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(cleanup_reader), os.fdopen(cleanup_read_fd, "rb"))
    boot_id = uuid4().hex
    # 日志写入文件，避免等待就绪或退出时填满无人读取的管道。
    with (
        closing(transport),
        closing(cleanup_transport),
        tempfile.TemporaryFile() as output,
        tempfile.TemporaryFile() as error,
    ):
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable, str(ROOT / "main.py"), "gateway", "--config", str(config),
                "--workspace", str(workspace), pass_fds=(write_fd, cleanup_write_fd),
                env={**env, "AKASHIC_SUPERVISED": "1", "AKASHIC_BOOT_ID": boot_id,
                     "AKASHIC_RESTART_NONCE": uuid4().hex, "AKASHIC_LIFECYCLE_FD": str(write_fd),
                     "SCENARIO_CLEANUP_FD": str(cleanup_write_fd)},
                stdin=asyncio.subprocess.PIPE, stdout=output, stderr=error)
        finally:
            os.close(write_fd)
            os.close(cleanup_write_fd)
        try:
            async with asyncio.timeout(20):
                while True:
                    line = await reader.readline()
                    assert line, "runtime 在完整就绪前关闭生命周期管道"
                    if json.loads(line)["type"] == "ready":
                        break
            # 2. 真实第二进程不能抢占 workspace，也不能从别的 workspace 共用发布 owner。
            for target, conflict in (
                (workspace, "workspace 已由其他 runtime 占用"),
                (peer_workspace, "plugin-home 已有发布或消费 owner"),
            ):
                contender = await asyncio.create_subprocess_exec(
                    sys.executable, str(ROOT / "main.py"), "gateway", "--config", str(config),
                    "--workspace", str(target), env=env,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                try:
                    stdout, stderr = await asyncio.wait_for(contender.communicate(), 20)
                    assert contender.returncode != 0 and conflict in stderr.decode(), (stdout, stderr)
                finally:
                    if contender.returncode is None:
                        contender.kill()
                        await contender.communicate()
                assert (workspace / ".runtime-ready.json").exists()
            # 3. 等插件进入清理后重发 SIGTERM，再释放其清理步骤。
            process.send_signal(signal.SIGTERM)
            assert await asyncio.wait_for(cleanup_reader.readexactly(1), 20) == b"1"
            process.send_signal(signal.SIGTERM)
            assert process.stdin is not None
            process.stdin.write(b"\n")
            await process.stdin.drain()
            await asyncio.wait_for(process.wait(), 20)
            output.seek(0)
            error.seek(0)
            assert process.returncode == 0, (process.returncode, output.read()[-4096:], error.read()[-4096:])
            assert not (workspace / ".runtime-ready.json").exists()
            closed = json.loads((workspace / "runtime/closed" / f"{boot_id}.json").read_text())
            assert closed["state"] == "closed" and closed["pid"] == process.pid
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            assert process.stdin is not None
            process.stdin.close()


async def run(base: Path) -> dict[str, bool]:
    """启动真实 main.py 子进程；管道协调退出，不使用 sleep。"""
    from agent.plugins.install import install_git_plugin
    from bootstrap.init_workspace import init_workspace
    from plugins.ledger.log import MessageLog
    from plugins.ledger.contract import Input

    home, workspace, config = base / "home", base / "workspace", base / "config.toml"
    env = {**os.environ, "HOME": str(home), "AKASHIC_PLUGIN_HOME": str(home),
           "AKASHIC_PLUGIN_DISTRIBUTION": "", "AKASHIC_EXTRA_PLUGIN_DIRS": "",
           "AKASHIC_EXECUTION_MODE": "local", "AKASHIC_SUPERVISED": "0",
           "PYTHONPATH": os.pathsep.join([str(ROOT), str(ROOT / "sdk/python/src")])}
    os.environ.update({key: env[key] for key in ("HOME", "AKASHIC_PLUGIN_HOME", "AKASHIC_PLUGIN_DISTRIBUTION")})
    config.write_text('[runtime]\n')
    init_workspace(config_path=config, workspace=workspace)
    source = base / "source"
    source.mkdir()
    (source / "plugin.py").write_text(PROVIDER)
    subprocess.run(["git", "init", "-q", "--initial-branch=source", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Scenario", "-c",
                    "user.email=scenario@example.invalid", "commit", "-qm", "scenario"], check=True)
    install_git_plugin(workspace=workspace, source=str(source), marketplace="lab", plugins_home=home)
    log = MessageLog(workspace / "sessions.db")
    log.writer("saved", author="user", source="saved", body_types=(Input,), content={}).append("kept", Input(()))
    log.close()
    with sqlite3.connect(workspace / "sessions.db") as db:
        rows = db.execute("SELECT * FROM messages ORDER BY rowid").fetchall()
    for intent, failed in ((b"done\n", False), (b"fail\n", True), (b"", False)):
        process = await asyncio.create_subprocess_exec(sys.executable, str(ROOT / "main.py"), "gateway",
            "--config", str(config), "--workspace", str(workspace), env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            assert process.stdout is not None
            async with asyncio.timeout(20):
                while await process.stdout.readline() != b"SCENARIO_READY\n":
                    assert process.returncode is None, "runtime 在就绪前退出"
            output, error = await asyncio.wait_for(process.communicate(intent), 20)
            assert (process.returncode != 0) is failed, (output, error)
            assert b"ADMISSION_CLOSED" in output
            if failed:
                assert b"native owner failed" in error
        finally:
            if process.returncode is None:
                process.kill()
                await process.communicate()
        assert json.loads((workspace / "runtime/endpoints.json").read_text())["endpoints"] == []
        with sqlite3.connect(workspace / "sessions.db") as db:
            assert db.execute("SELECT * FROM messages ORDER BY rowid").fetchall() == rows
    peer_workspace = base / "peer-workspace"
    init_workspace(config_path=config, workspace=peer_workspace)
    await stop_ready_process(config, workspace, peer_workspace, env)
    assert json.loads((workspace / "runtime/endpoints.json").read_text())["endpoints"] == []
    with sqlite3.connect(workspace / "sessions.db") as db:
        assert db.execute("SELECT * FROM messages ORDER BY rowid").fetchall() == rows
    return {"native_process_stop": True, "admission_closed": True,
            "failure_exit_and_visible_reason": True, "eof_exit_zero": True,
            "restart_after_failure": True, "messages_preserved": True,
            "sigterm_after_ready": True, "repeated_sigterm_during_cleanup": True,
            "readiness_cleared_and_closed": True,
            "workspace_conflict_rejected": True, "publication_conflict_rejected": True}


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="process-shutdown-") as path:
        print(json.dumps(asyncio.run(run(Path(path)))))
