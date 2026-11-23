"""Preview the configuration handoff to upstream Watch and Default Memory."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from agent.plugin_composition.config_input import config_refs, load_config
from plugins.default_memory.plugin import Config
from plugins.default_memory.scopes import ScopePolicy


def _read_selection(workspace: Path) -> dict[str, tuple[str, Path]]:
    rows = json.loads((workspace / "runtime/plugin-stable.json").read_text())["inputs"].values()
    selected: dict[str, tuple[str, Path]] = {}
    for row in rows:
        identity = row["plugin_id"]
        name, _, market = identity.partition("@")
        if name not in {"watch", "wake", "drift", "default_memory", "akasha"}:
            continue
        path = workspace / row["data_dir"]
        if (name in selected or path.parent != workspace / "plugin-data"
                or path.name not in {name, f"{name}-{market or 'builtin'}"} or path.resolve() != path):
            raise ValueError(f"不支持的数据目录或重复插件身份: {identity}")
        selected[name] = identity, path
    return selected


def _scope_policies(ledger: Path, identity: str) -> dict[str, dict[str, Any]]:
    with closing(sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute("SELECT key, value FROM owner_records WHERE owner=? ORDER BY key",
                          ("plugin:" + identity + ":scope-policy",))
        return {key: ScopePolicy.model_validate_json(value).model_dump() for key, value in rows}


def preview(workspace: Path) -> dict[str, Any]:
    workspace = workspace.resolve(strict=True)
    selected = _read_selection(workspace)
    configs = {name: load_config(path)[0] for name, (_, path) in selected.items()}
    if any(config_refs(configs.get(name)) for name in ("default_memory", "watch", "wake", "drift")):
        raise ValueError("旧设置含凭据引用，须由原配置 owner 单独处理")

    memory = configs.get("default_memory", {})
    previous_akasha = selected.get("akasha")
    if previous_akasha is not None:
        policies = _scope_policies(workspace / "sessions.db", previous_akasha[0])
        if "scope_policies" in memory and memory["scope_policies"] != policies:
            raise ValueError("Default Memory 已有不同的作用域策略")
        memory = {**memory, "scope_policies": policies}
    memory = {**memory, "enabled": True, "mode": "active"}
    _ = Config.model_validate(memory)

    watch = configs.get("watch", {})
    drift = configs.get("drift", {})
    wake = configs.get("wake", {})
    manual: list[str] = []
    if wake:
        if "target" in watch:
            raise ValueError("Watch 已有上游目标，须核对后再迁移")
        supported: dict[str, Any] = {"timezone": wake.get("timezone", "Asia/Shanghai")}
        target = wake.get("delivery")
        if wake.get("enabled", True) is True and isinstance(target, dict):
            exact = f"{target.get('channel', '')}:{target.get('recipient', '')}"
            if target.get("session_id") == exact and all(target.get(field) for field in ("channel", "recipient")):
                supported["target"] = exact
            else:
                manual.append("verify_exact_watch_target")
        interval = watch.get("interval_seconds")
        if interval is not None:
            if type(interval) is int and 60 <= interval <= 14400 and interval % 60 == 0:
                supported["interval_minutes"] = interval // 60
            else:
                manual.append("choose_watch_polling_interval")
        if type(watch.get("digest_hours")) is int:
            supported["digest_hours"] = watch["digest_hours"]
        if isinstance(watch.get("model"), str) and watch["model"]:
            supported["watch_models"] = [watch["model"]]
        if watch.get("orders"):
            manual.append("review_existing_watch_orders")
        if drift.get("auto_propose") or drift.get("memory_assisted"):
            manual.append("review_legacy_timer_pause_and_memory_consent")
        if wake.get("active_sources"):
            manual.append("review_legacy_active_source_policy")
        watch = supported

    return {
        "default_memory": {"current_identity": selected.get("default_memory", (None,))[0], "config": memory},
        "watch": {"current_identity": selected.get("watch", (None,))[0], "config": watch},
        "existing_paths": {name: str(path.relative_to(workspace)) for name, (_, path) in selected.items()},
        "review": manual,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    plan = preview(args.workspace)
    if args.output is not None:
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(plan, file, ensure_ascii=False, indent=2)
            file.write("\n")
    print(json.dumps({
        "default_memory": plan["default_memory"]["current_identity"],
        "scope_policies": len(plan["default_memory"]["config"].get("scope_policies", {})),
        "watch_target_mapped": bool(plan["watch"]["config"].get("target")),
        "review": plan["review"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
