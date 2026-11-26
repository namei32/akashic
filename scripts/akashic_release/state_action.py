"""Unprivileged child of the installed publisher; no Docker or sudo capability."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def main() -> None:
    if os.geteuid() == 0:
        raise PermissionError("应用状态操作必须降为运行用户")
    action = sys.argv[1]
    payload = json.load(sys.stdin)
    result = None
    if action == "capture":
        from scripts.akashic_release.snapshot import capture_state
        result = capture_state(Path(payload["state"]) / "workspace", Path(payload["state"]) / "plugin-home", [])
    elif action == "restore":
        from scripts.akashic_release.activate import _restore_selection
        from scripts.akashic_release.snapshot import restore_state
        state = Path(payload["state"])
        if payload["selection"] is not None:
            _restore_selection(state / "workspace/runtime/plugin-stable.json", payload["selection"],
                               payload["published"], payload["publishing"])
        if payload["snapshot"] is not None:
            restore_state(payload["snapshot"])
    elif action == "directories":
        from scripts.akashic_release.activate import _prepare_workload_dirs
        from scripts.akashic_release.model import ReleasePaths
        _prepare_workload_dirs(ReleasePaths(Path(payload["state"]).parent, state_root=Path(payload["state"])))
    elif action == "finish":
        from scripts.first_install import finish_first_install
        state = Path(payload["state"])
        finish_first_install(state, state / "workspace")
    elif action == "read-object":
        from scripts.akashic_release.manifest import read_json
        result = read_json(Path(payload["path"]))
    else:
        raise ValueError("未知的应用状态操作")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
