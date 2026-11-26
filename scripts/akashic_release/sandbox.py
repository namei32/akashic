"""Installed Host Bridge launcher. Its children inherit a private process/filesystem view."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys


def main() -> None:
    path = Path("/etc/akashic-publisher/runtime.json")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise PermissionError("运行隔离配置必须由管理员持有")
    profile = json.loads(path.read_text())
    if os.geteuid() != profile["uid"] or os.getegid() != profile["gid"]:
        raise PermissionError("Host Bridge 没有使用独立运行账号")
    if not sys.argv[1:]:
        raise ValueError("缺少运行命令")
    arguments = ["/usr/bin/bwrap", "--die-with-parent", "--unshare-user", "--unshare-pid", "--unshare-ipc",
                 "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/run",
                 "--tmpfs", "/tmp", "--tmpfs", "/mnt", "--tmpfs", "/home"]
    for source, target in profile["readonly"]:
        arguments += ["--ro-bind", source, target]
    for source, target in profile["writable"]:
        arguments += ["--bind", source, target]
    arguments += ["--setenv", "HOME", profile["home"], "--unsetenv", "WSL_INTEROP", "--unsetenv", "WSLENV",
                  "--unsetenv", "DBUS_SESSION_BUS_ADDRESS", "--unsetenv", "SSH_AUTH_SOCK",
                  "--chdir", profile["home"], "--", *sys.argv[1:]]
    os.execv(arguments[0], arguments)


if __name__ == "__main__":
    main()
