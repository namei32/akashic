"""Administrator-owned publisher policy and immutable release approvals."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import stat
from typing import Any

POLICY = Path("/etc/akashic-publisher/policy.json")
_SHA = re.compile(r"[0-9a-f]{40}")


def protected(path: Path) -> Path:
    """Reject writable ancestors as well as symlinks, including a writable home directory."""
    if not path.is_absolute():
        raise ValueError("发布路径必须为绝对路径")
    for item in (*reversed(path.parents), path):
        info = item.lstat()
        if info.st_uid != 0 or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode):
            raise PermissionError(f"发布控制路径不受管理员保护: {item}")
    return path


def read_policy() -> dict[str, Any]:
    policy = json.loads(protected(POLICY).read_text())
    if policy.get("version") != 1 or type(policy.get("runtime_uid")) is not int or policy["runtime_uid"] <= 0:
        raise ValueError("发布策略版本或运行用户无效")
    for key in ("root", "runtime_env", "publisher"):
        protected(Path(policy[key]))
    return policy


def approval(policy: dict[str, Any], commit: str) -> dict[str, Any]:
    if not _SHA.fullmatch(commit):
        raise ValueError("发布版本必须是完整提交")
    root = Path(policy["root"])
    approved = json.loads(protected(root / "approved" / f"{commit}.json").read_text())
    manifest = protected(root / "releases" / f"{commit}.json")
    if approved.get("manifest_sha256") != hashlib.sha256(manifest.read_bytes()).hexdigest():
        raise PermissionError("制品清单与管理员批准的摘要不符")
    release = json.loads(manifest.read_text())
    if release.get("sourceCommit") != commit or release.get("imageId") != approved.get("image_id"):
        raise PermissionError("制品身份不属于批准版本")
    protected(root / "runtime-sources" / commit)
    protected(root / "bridge-venvs" / commit)
    return release
