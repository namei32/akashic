"""Keep application execution and mutable state operations outside publisher privileges."""
from __future__ import annotations

import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
from typing import Any


def policy() -> dict[str, Any] | None:
    if not os.environ.get("AKASHIC_PUBLISHER_POLICY"):
        return None
    from scripts.akashic_release.authority_policy import POLICY, read_policy
    if os.geteuid() != 0 or os.environ["AKASHIC_PUBLISHER_POLICY"] != str(POLICY):
        raise PermissionError("受保护发布模式只能由宿主入口选择")
    return read_policy()


def state_action(action: str, payload: dict[str, object]) -> Any:
    configured = policy()
    if configured is None:
        raise RuntimeError("未启用受保护发布")
    account = pwd.getpwuid(configured["runtime_uid"])
    environment = {"HOME": account.pw_dir, "PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(Path(__file__).with_name("state_action.py")), action],
        input=json.dumps(payload), capture_output=True, text=True, check=True, cwd="/",
        env=environment, user=account.pw_uid, group=account.pw_gid, extra_groups=[],
    )
    return json.loads(result.stdout)


def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Fixed Docker layouts use administrator-pinned bind roots, never caller-owned path traversal."""
    configured = policy()
    if configured is None:
        raise RuntimeError("未启用受保护发布")
    command = list(command)
    if command[0] == "docker":
        command[0] = "/usr/bin/docker"
    if command[:2] == ["/usr/bin/docker", "run"]:
        from scripts.akashic_release.authority_policy import protected
        state = configured["state"]
        roots = configured["mounts"]
        for index, argument in enumerate(command):
            if argument != "--mount":
                continue
            fields = command[index + 1].split(",")
            source = next(item[4:] for item in fields if item.startswith("src="))
            if source in roots:
                source = roots[source]
                protected(Path(source).parent)
                if Path(source).is_symlink() or not os.path.ismount(source):
                    raise PermissionError("发布数据源必须是管理员固定的挂载点")
            elif source == state or Path(source).is_relative_to(state):
                raise PermissionError("未批准的数据挂载")
            else:
                protected(Path(source))
                if not Path(source).is_relative_to(configured["root"]):
                    raise PermissionError("发布输入不属于固定发布目录")
            command[index + 1] = ",".join("src=" + source if item.startswith("src=") else item for item in fields)
        account = pwd.getpwuid(configured["runtime_uid"])
        command[2:2] = ["--user", f"{account.pw_uid}:{account.pw_gid}", "--cap-drop", "ALL",
                        "--security-opt", "no-new-privileges:true"]
    return subprocess.run(command, **kwargs)
