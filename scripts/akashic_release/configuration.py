"""从显式实例环境解析发布位置，不将既有实例切到默认数据目录。"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from scripts.akashic_release.doctor import read_environment
from scripts.akashic_release.model import ReleasePaths

DEFAULT_ROOT = Path("/srv/data/services/akashic")


def runtime_environment() -> Path:
    return Path(os.environ.get("AKASHIC_RUNTIME_ENV") or Path.home() / ".config/akashic-container/runtime.env").expanduser()


def _release_root(values: dict[str, str]) -> Path | None:
    manifest = values.get("AKASHIC_RELEASE_MANIFEST")
    if not manifest:
        return None
    path = Path(manifest)
    if not path.is_absolute() or path.parent.name != "releases":
        raise RuntimeError("AKASHIC_RELEASE_MANIFEST 必须位于发布根的 releases 目录")
    return path.parent.parent.resolve()


def resolve_arguments(args: argparse.Namespace) -> None:
    """命令行优先；已有实例的缺省值来自同一份 runtime.env。"""
    if not any(hasattr(args, name) for name in ("root", "runtime_env", "mise")):
        return
    environment = getattr(args, "runtime_env", None) or runtime_environment()
    args.runtime_env = environment.expanduser().resolve()
    values = read_environment(args.runtime_env) if args.runtime_env.is_file() else {}
    if hasattr(args, "root") and args.root is None:
        args.root = _release_root(values) or DEFAULT_ROOT
    if hasattr(args, "mise") and args.mise is None:
        args.mise = Path(values.get("AKASHIC_MISE") or Path.home() / ".local/bin/mise")


def release_paths(root: Path, environment_file: Path) -> ReleasePaths:
    """原数据仍在原位置；不创建另一套空 workspace 或插件目录。"""
    values = read_environment(environment_file) if environment_file.is_file() else {}
    configured_root = _release_root(values)
    if configured_root is not None and configured_root != root.resolve():
        raise RuntimeError("runtime.env 属于另一发布根；请核对 --root 与 --runtime-env")
    workspace = values.get("AKASHIC_WORKSPACE")
    if not workspace:
        return ReleasePaths(root.resolve())
    if not Path(workspace).is_absolute():
        raise RuntimeError("AKASHIC_WORKSPACE 必须是绝对路径")
    state = Path(workspace).parent.resolve()
    expected = {"AKASHIC_WORKSPACE": state / "workspace", "AKASHIC_CONFIG": state / "config.toml",
                "AKASHIC_PLUGIN_HOME": state / "plugin-home", "AKASHIC_WORKLOAD_RUNTIME_DIR": state / "workload-runtime"}
    for key, path in expected.items():
        if values.get(key) and Path(values[key]).resolve() != path:
            raise RuntimeError(f"既有 {key} 不属于同一状态目录；不能隐式迁移")
    return ReleasePaths(root.resolve(), state_root=state)
