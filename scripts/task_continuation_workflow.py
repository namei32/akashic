"""Run the Jiujiu scenario against installed plugins, with a local deterministic provider."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

from agent.plugin_composition import ServiceKey
from plugins.jobs.contract import JOB_NOTICES, JobOutcome, JobWatch
from plugins.ledger.contract import MESSAGE_CATALOG, Output
from tests.test_background_jobs import _job_sources, _rows_until
from tests.test_default_reply import application


GOALS = ("确认 CI", "修复 B 组并复验", "发布器接真实宿主")
DRIVER = '''
            calls.append(request)
            messages = request.messages
            history = str(messages)
            def update(*items):
                return LLMResponse(None, [ToolCall("progress", "update_task", {"items": [
                    {"goal": name, "status": status, "detail": detail, "job_id": job}
                    for name, status, detail, job in items]})])
            ci, repair, release = GOALS
            if not any("明确阻塞" in str(m.get("tool_calls", "")) for m in messages):
                return update((ci, "ready", "启动隔离 CI", ""), (repair, "ready", "准备 B 组复验", ""),
                              (release, "blocked", "明确阻塞：真实宿主权限待确认", ""))
            if "发布器原型通过，真实宿主尚待接入。" not in history:
                return LLMResponse("发布器原型通过，真实宿主尚待接入。")
            if not any("start_job" in str(m.get("tool_calls", "")) for m in messages):
                return LLMResponse(None, [ToolCall("ci", "start_job", {})])
            if "后台任务 fake-1" not in history:
                if "隔离 CI 已启动" not in history:
                    return update((ci, "waiting", "隔离 CI 已启动", "fake-1"),
                                  (repair, "waiting", "使用本次 CI 生成的对照结果继续", "fake-1"))
                return LLMResponse("已派发 CI。")
            if "CI 已绿" not in history:
                return update((ci, "done", "CI 已绿", ""), (repair, "ready", "修复并验证召回", ""))
            if "CI 已通过，发布器仍受阻。" not in history:
                return LLMResponse("CI 已通过，发布器仍受阻。")
            tool_results = [str(m.get("content")) for m in messages if m.get("role") == "tool"]
            if any("结果未知" in value for value in tool_results) and not any("verified one write" in value for value in tool_results):
                return LLMResponse(None, [ToolCall("inspect", "inspect_evidence", {})])
            if not any("written" in value or "verified one write" in value for value in tool_results):
                return LLMResponse(None, [ToolCall("repair", "write_evidence", {})])
            if CRASH == "after-result" and not Path(BOOT_MARK).exists():
                # The real tool result has reached the next model request. No graceful cleanup.
                Path(BOOT_MARK).touch()
                os._exit(75)
            if "B 组复验证据已落盘" not in history:
                return update((repair, "done", "B 组复验证据已落盘", ""))
            return LLMResponse("B 组复验完成；发布器仍待宿主权限。")
'''


def sources(path: Path, crash: str) -> None:
    _job_sources(path)
    provider = path / "test_provider/plugin.py"
    text = provider.read_text()
    begin = text.index("            calls.append(request)")
    end = text.index("    descriptor = ", begin)
    driver = DRIVER.replace("GOALS", repr(GOALS)).replace("CRASH", repr(crash))
    driver = driver.replace("BOOT_MARK", repr(str(path.parent / "crashed")))
    text = "import os\n" + text[:begin] + driver + text[end:]
    if crash == "before-result":
        text = text.replace('            return Result("success", (ContentPart("text", "written"),))',
                            '            os._exit(75)  # effect.txt closed, no ToolResult yet')
    read_tool = '''
    class Inspect(Target):
        async def invoke(self, key, args):
            assert Path(EFFECT).read_text().splitlines() == ["once"]
            return Result("success", (ContentPart("text", "verified one write"),))
    @asynccontextmanager
    async def open_inspect(state):
        yield Inspect()
    await ctx.require(TOOLS).register(ctx, name="inspect_evidence", description="Read back the isolated file",
        parameters={"type": "object"}, open=open_inspect)
'''.replace("EFFECT", repr(str(path.parent / "effect.txt")))
    provider.write_text(text.replace("    await ctx.provide(CHAT_MODELS", read_tool + "\n    await ctx.provide(CHAT_MODELS"))


async def exercise(folder: Path, host, log, fresh: bool):
    from datetime import UTC, datetime
    from plugins.channels.contract import CHANNEL_INPUT_V2, ChannelInboundMessage
    ctx = host.live_root.context
    calls = ctx.require(ServiceKey("fixture.calls"))
    async def duplicate():
        return JobOutcome("completed", "exit_code=0")
    def redeliver():
        ctx.require(JOB_NOTICES).watch(JobWatch("test:room", "conversation", "fake-1", "shell",
                                                "fixture job", "fixture result is ready.", duplicate))
    if fresh:
        await ctx.require(CHANNEL_INPUT_V2)("test:room", "request", ChannelInboundMessage(
            "test", "user", "room", "完成 CI、B 组复验、发布器接管；先做能做的事项。",
            datetime.now(UTC), {}))
        await _rows_until(log, lambda rows: any(isinstance(r.body, Output) and r.body.finish == "complete"
                                                and r.source == "conversation" for r in rows))
        ctx.require(ServiceKey("fixture.job_gate")).set()
        redeliver()
    rows = await _rows_until(log, lambda rows: any(isinstance(r.body, Output) and r.body.finish == "complete"
                                                   and r.source == "job:fake-1" for r in rows))
    partial = [r for r in rows if isinstance(r.body, Output) and any(
        getattr(p, "value", None) == "CI 已通过，发布器仍受阻。" for p in r.body.parts)]
    assert len(partial) == 1 and partial[0].body.finish == "continue"
    assert any(r.source == "conversation" and isinstance(r.body, Output) and r.body.finish == "continue"
               and any(getattr(p, "value", None) == "发布器原型通过，真实宿主尚待接入。" for p in r.body.parts)
               for r in rows)
    assert any(getattr(p, "value", None) == "B 组复验完成；发布器仍待宿主权限。" for p in rows[-1].body.parts)
    count = len(calls)
    for _ in range(2):
        redeliver()
    await asyncio.sleep(0.4)
    assert len(calls) == count and log.reader("test:room").head() == rows[-1].seq
    assert (folder / "effect.txt").read_text().splitlines() == ["once"]


async def boot(folder: Path, phase: str, crash: str) -> None:
    if phase == "first":
        async with application(folder, replying=True, extra_sources=lambda p: sources(p, crash)) as (log, host):
            await exercise(folder, host, log, True)
    else:
        from agent.plugins.manager import PluginManager
        host = PluginManager([folder / "plugins"], workspace=folder / "workspace", installed_cache_root=folder / "home/cache")
        try:
            await host.load_all()
            log = host.live_root.context.require(MESSAGE_CATALOG)._log
            await exercise(folder, host, log, False)
        finally:
            await host.terminate_all()


def snapshot(folder: Path):
    with sqlite3.connect(f"file:{folder / 'workspace/sessions.db'}?mode=ro", uri=True) as db:
        return db.execute("SELECT * FROM messages WHERE session_key='test:room' ORDER BY seq").fetchall()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path)
    parser.add_argument("--phase", choices=["first", "restart"])
    parser.add_argument("--crash", choices=["none", "before-result", "after-result"], default="none")
    args = parser.parse_args()
    if args.phase:
        asyncio.run(boot(args.folder, args.phase, args.crash))
        return
    for crash in ("none", "after-result", "before-result"):
        with tempfile.TemporaryDirectory(prefix="jiujiu-") as name:
            folder = Path(name)
            env = {**os.environ, "HOME": name, "XDG_CONFIG_HOME": str(folder / "config")}
            cmd = [sys.executable, "-m", "scripts.task_continuation_workflow", "--folder", name, "--phase"]
            first = subprocess.run([*cmd, "first", "--crash", crash], env=env, timeout=90)
            assert first.returncode == (0 if crash == "none" else 75), first.returncode
            before = snapshot(folder)
            assert (folder / "effect.txt").read_text().splitlines() == ["once"]
            subprocess.run([*cmd, "restart"], env=env, check=True, timeout=90)
            after = snapshot(folder)
            assert before and after[:len(before)] == before
            assert sum(row[4] == "user" for row in after) == 1
            assert after == before if crash == "none" else len(after) > len(before)
            bodies = [json.loads(row[6]) for row in after]
            if crash == "before-result":
                assert any("结果未知" in str(body) and body.get("outcome") == "error" for body in bodies)
                assert any("verified one write" in str(body) and body.get("outcome") == "success" for body in bodies)
            print(json.dumps({"fault": crash, "user_inputs": 1, "protected_rows": len(before),
                              "final_rows": len(after), "file_appends": 1, "duplicate_callbacks_on_reopen": 2}))


if __name__ == "__main__":
    main()
