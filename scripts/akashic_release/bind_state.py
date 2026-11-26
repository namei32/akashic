"""Attach stable, administrator-named bind mounts without following app symlinks."""
from __future__ import annotations

from contextlib import ExitStack
import os
from pathlib import Path
import subprocess


def main() -> None:
    root = Path("/var/lib/akashic-publisher/mounts")
    mounted = {line.split()[4] for line in Path("/proc/self/mountinfo").read_text().splitlines()}
    for marker in sorted(root.glob("*.source")):
        target = marker.with_suffix("")
        if str(target) in mounted:
            continue
        source = Path(marker.read_text().strip())
        if not source.is_absolute() or ".." in source.parts:
            raise ValueError(f"挂载源必须是绝对目录: {source}")
        with ExitStack() as stack:
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
            descriptor = os.open("/", flags)
            stack.callback(os.close, descriptor)
            for part in source.parts[1:]:
                descriptor = os.open(part, flags, dir_fd=descriptor)
                stack.callback(os.close, descriptor)
            subprocess.run(["/usr/bin/mount", "--no-canonicalize", "--bind",
                            f"/proc/self/fd/{descriptor}", str(target)],
                           pass_fds=(descriptor,), check=True)


if __name__ == "__main__":
    main()
