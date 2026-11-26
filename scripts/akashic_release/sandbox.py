"""Installed Host Bridge launcher. Its children inherit a private process/filesystem view."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import signal
import subprocess
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
                 "--disable-userns", "--ro-bind", "/usr", "/usr", "--proc", "/proc", "--dev", "/dev",
                 "--tmpfs", "/run", "--tmpfs", "/tmp"]
    for name in ("bin", "sbin", "lib", "lib64"):
        if Path("/" + name).is_symlink():
            arguments += ["--symlink", "usr/" + name, "/" + name]
    for name in ("passwd", "group", "hosts", "nsswitch.conf", "resolv.conf", "ssl", "localtime"):
        source = Path("/etc") / name
        if source.exists():
            arguments += ["--ro-bind", str(source.resolve()), str(source)]
    for source, target in profile["readonly"]:
        arguments += ["--ro-bind", source, target]
    for source, target in profile["writable"]:
        arguments += ["--bind", source, target]
    arguments += ["--setenv", "HOME", profile["home"], "--unsetenv", "WSL_INTEROP", "--unsetenv", "WSLENV",
                  "--unsetenv", "DBUS_SESSION_BUS_ADDRESS", "--unsetenv", "SSH_AUTH_SOCK",
                  "--chdir", profile["home"], "--", *sys.argv[1:]]
    # systemd signals the whole cgroup. The Bridge handles TERM and releases its
    # leases. Keep the launcher and bwrap alive to propagate its real exit code.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    result = subprocess.run(arguments, check=False)
    raise SystemExit(result.returncode if result.returncode >= 0 else 128 - result.returncode)


if __name__ == "__main__":
    main()
