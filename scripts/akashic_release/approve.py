"""Administrator-only import of an already built release; never builds or runs candidate code."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.akashic_release.authority_policy import approval, protected, read_policy
from scripts.akashic_release.doctor import read_environment
from scripts.akashic_release.manifest import release_lock, write_json


def seal(directory: Path) -> None:
    for path in (directory, *directory.rglob("*")):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            os.chown(path, 0, 0, follow_symlinks=False)
        elif stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode):
            os.chown(path, 0, 0)
            path.chmod(0o755 if stat.S_ISDIR(info.st_mode) or info.st_mode & 0o111 else 0o644)
        else:
            raise ValueError(f"制品含非常规文件: {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="管理员导入并批准已构建的完整版本")
    parser.add_argument("--commit", required=True)
    parser.add_argument("--prepared-root", type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0 or not re.fullmatch(r"[0-9a-f]{40}", args.commit):
        raise PermissionError("仅管理员可批准完整提交")
    policy = read_policy()
    root = Path(policy["root"])
    manifest_bytes = (args.prepared_root / "releases" / f"{args.commit}.json").read_bytes()
    release = json.loads(manifest_bytes)
    if (release.get("sourceCommit") != args.commit or release.get("schemaVersion") not in {1, 2}
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", release.get("imageId", ""))
            or not re.fullmatch(r"[0-9a-f]{40}", release.get("sourceTree", ""))):
        raise ValueError("待批准制品身份无效")
    actual = subprocess.check_output(["/usr/bin/docker", "image", "inspect", release["imageId"],
                                      "--format", "{{.Id}}"], text=True).strip()
    if actual != release["imageId"]:
        raise ValueError("镜像尚未导入或摘要不符")
    with release_lock(root / "run/release.lock"):
        destination = root / "releases" / f"{args.commit}.json"
        catalog = root / "approved" / f"{args.commit}.json"
        if catalog.exists():
            previous = approval(policy, args.commit)
            if previous != release:
                raise RuntimeError("同一提交已经批准另一份制品")
            print(json.dumps({"status": "already-approved", "commit": args.commit}))
            return
        source = root / "runtime-sources" / args.commit
        if source.exists():
            raise RuntimeError("有未完成的制品导入，需管理员检查；不覆盖现场")
        source.mkdir(parents=True)
        environment = {"PATH": "/usr/bin:/bin", "HOME": str(root), "GIT_CONFIG_NOSYSTEM": "1",
                       "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0"}
        for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
            if os.environ.get(key):
                environment[key] = os.environ[key]
        def git(*arguments: str) -> str:
            return subprocess.check_output(["/usr/bin/git", "-c", "core.hooksPath=/dev/null", *arguments],
                                           cwd=source, env=environment, text=True).strip()
        git("init", "--quiet")
        git("remote", "add", "origin", policy["origin"])
        git("fetch", "--quiet", "--depth=1", "origin", args.commit)
        git("checkout", "--quiet", "--detach", args.commit)
        if git("rev-parse", "HEAD^{tree}") != release["sourceTree"]:
            raise RuntimeError("Git 来源与制品源码树不符")
        for path in source.rglob("*"):
            if path.is_symlink() and not path.resolve().is_relative_to(source):
                raise ValueError("宿主源码包含越界软链接")
        bridge = root / "bridge-venvs" / args.commit
        shutil.copytree((args.prepared_root / "bridge-venvs" / args.commit).resolve(strict=True), bridge,
                        symlinks=True, ignore=shutil.ignore_patterns("__pycache__"))
        venv_config = bridge / "pyvenv.cfg"
        prefix = Path(read_environment(Path(policy["runtime_env"]))["AKASHIC_HOST_PYTHON_PREFIX"])
        protected(prefix)
        config = venv_config.read_text()
        old_base = next(line.partition(" = ")[2] for line in config.splitlines() if line.startswith("home = "))
        old_prefix = str(Path(old_base).parent)
        venv_config.write_text(config.replace(old_prefix + "/", str(prefix) + "/"))
        for path in bridge.rglob("*"):
            if path.is_symlink() and (link := os.readlink(path)).startswith(old_prefix + "/"):
                path.unlink()
                path.symlink_to(str(prefix) + link[len(old_prefix):])
        old_source = str(args.prepared_root / "runtime-sources" / args.commit)
        for path in bridge.rglob("*.pth"):
            if not path.is_symlink():
                text = path.read_text()
                path.write_text(text.replace(old_source, str(source)))
        seal(source)
        seal(bridge)
        for path in bridge.rglob("*"):
            if path.is_symlink():
                protected(path.resolve(strict=True))
        destination.write_bytes(manifest_bytes)
        destination.chmod(0o644)
        protected(destination)
        write_json(catalog, {"commit": args.commit, "image_id": release["imageId"],
                             "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest()})
        print(json.dumps({"status": "approved", "commit": args.commit, "imageId": actual}))


if __name__ == "__main__":
    main()
