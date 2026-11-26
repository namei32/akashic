#!/usr/bin/python3 -I
"""管理员安装的固定入口：仅给独立部署 worker 补充 Docker 组，随后以普通用户执行。"""
from __future__ import annotations

import grp
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys

PROFILE = Path("/etc/akashic/deploy-worker.json")


def owned_file(path: Path, owners: set[int]) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid not in owners or info.st_mode & 0o022:
            raise RuntimeError("文件属主或权限不符合部署入口要求")
        if info.st_size > 65536:
            raise RuntimeError("部署入口文件过大")
        return os.read(descriptor, 65537).decode("utf-8")
    finally:
        os.close(descriptor)


def main() -> None:
    if os.geteuid() != 0:
        raise RuntimeError("通过管理员安装的精确 sudo 规则调用")
    arguments = sys.argv[1:]
    if arguments != ["check"] and not (
        len(arguments) == 2 and arguments[0] == "run" and re.fullmatch(r"[0-9a-f]{32}", arguments[1])
    ):
        raise RuntimeError("只接受 check 或 run <32 位请求 ID>")
    profile = json.loads(owned_file(PROFILE, {0}))
    account = pwd.getpwnam(profile["user"])
    if account.pw_uid == 0 or os.environ.get("SUDO_UID") != str(account.pw_uid):
        raise RuntimeError("调用者不属于已配置的发布用户")
    root = Path(profile["root"]).resolve(strict=True)
    environment_file = Path(profile["runtime_env"]).resolve(strict=True)
    values: dict[str, str] = {}
    for raw in owned_file(environment_file, {0, account.pw_uid}).splitlines():
        if not raw.strip() or raw.startswith("#"):
            continue
        key, separator, value = raw.partition("=")
        if not separator or key in values:
            raise RuntimeError("实例环境文件格式无效")
        values[key] = value
    commit = values["AKASHIC_RUNTIME_COMMIT"]
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RuntimeError("实例提交身份无效")
    source = root / "runtime-sources" / commit
    python = root / "bridge-venvs" / commit / "bin/python"
    if Path(values["AKASHIC_RUNTIME_CHECKOUT"]) != source or Path(values["AKASHIC_BRIDGE_PYTHON"]) != python:
        raise RuntimeError("实例代码或解释器不属于配置的发布根")
    request_path = None
    if arguments[0] == "run":
        identity = arguments[1]
        if f"/akashic-deploy-{identity}.service" not in Path("/proc/self/cgroup").read_text():
            raise RuntimeError("worker 必须位于自己的 systemd service 中")
        request_path = root / "run/self-deploy" / f"{identity}.json"
        request = json.loads(owned_file(request_path, {account.pw_uid}))["request"]
        if (Path(request["root"]).resolve() != root
                or Path(request["runtimeEnv"]).resolve() != environment_file
                or Path(request["mise"]).resolve() != Path(values["AKASHIC_MISE"]).resolve()):
            raise RuntimeError("部署请求与固定实例不符")
    # 代码、解释器和 mise 全部在降权之后执行；不把 Docker 权限加到用户账户或 Host Bridge。
    os.initgroups(account.pw_name, account.pw_gid)
    os.setgroups(sorted({*os.getgroups(), grp.getgrnam("docker").gr_gid}))
    os.setgid(account.pw_gid)
    os.setuid(account.pw_uid)
    environment = {"HOME": account.pw_dir, "USER": account.pw_name, "LOGNAME": account.pw_name,
                   "PATH": "/usr/local/bin:/usr/bin:/bin", "XDG_RUNTIME_DIR": f"/run/user/{account.pw_uid}",
                   "AKASHIC_RUNTIME_ENV": str(environment_file), "PYTHONDONTWRITEBYTECODE": "1"}
    for key in ("PATH", "MISE_DATA_DIR", "MISE_CACHE_DIR", "MISE_CONFIG_DIR", "DOCKER_CONFIG",
                "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME",
                "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        if values.get(key):
            environment[key] = values[key]
    os.chdir(source)
    if arguments == ["check"]:
        if not os.access(root / "activation/active.json", os.R_OK):
            raise RuntimeError("发布用户无法读取当前激活回执")
        subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], env=environment,
                       check=True, stdout=subprocess.DEVNULL, timeout=20)
        print(json.dumps({"user": account.pw_name, "uid": os.getuid(), "root": str(root), "docker": "available"}))
        return
    os.execve(values["AKASHIC_MISE"], [values["AKASHIC_MISE"], "exec", "--", str(python), "-B", "-m",
              "scripts.akashic_release.cli", "self-deploy-worker", "--request", str(request_path)], environment)


if __name__ == "__main__":
    try:
        main()
    except (OSError, KeyError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"部署 worker 启动失败：{error}", file=sys.stderr)
        raise SystemExit(1)
