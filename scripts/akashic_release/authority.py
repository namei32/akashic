"""Socket-activated release authority; installed code and policy belong to the administrator."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import socket
import struct
import subprocess
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.akashic_release.authority_policy import POLICY, approval, protected, read_policy

SOCKET = "/run/akashic-publisher/control.sock"
_LIMIT = 65536
_ID = re.compile(r"[0-9a-f]{32}")


def exchange(message: dict[str, object], address: str = SOCKET) -> dict[str, Any]:
    encoded = json.dumps(message).encode() + b"\n"
    if len(encoded) > _LIMIT:
        raise ValueError("发布请求过大")
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(30)
        connection.connect(address)
        connection.sendall(encoded)
        with connection.makefile("rb") as stream:
            raw = stream.readline(_LIMIT + 1)
    if len(raw) > _LIMIT:
        raise ValueError("发布响应过大")
    response = json.loads(raw)
    if not response.get("ok"):
        raise RuntimeError(response.get("error", "发布请求失败"))
    return response["result"]


def _identity(value: object) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("发布任务编号无效")
    return value


def _submit(policy: dict[str, Any], message: dict[str, Any]) -> dict[str, Any]:
    from plugins.gateway.runtime_stop import StopParams
    from scripts.akashic_release.doctor import read_environment
    from scripts.akashic_release.manifest import read_json, release_lock, write_json
    from scripts.akashic_release.publisher_mode import state_action
    from scripts.akashic_release.self_deploy import _utc_now_iso

    if set(message) != {"action", "commit", "stop", "session"}:
        raise ValueError("发布请求只接受版本、停止凭据和通知会话")
    commit = message["commit"]
    approval(policy, commit)
    stop = StopParams.model_validate(message["stop"])
    identity = _identity(stop.request_id)
    session = message["session"]
    if not isinstance(session, str) or not 1 <= len(session) <= 512:
        raise ValueError("通知会话无效")
    root = Path(policy["root"])
    current = read_environment(Path(policy["runtime_env"]))
    ready = state_action("read-object", {"path": str(Path(current["AKASHIC_WORKSPACE"]) / ".runtime-ready.json")})
    if ready["bootId"] != stop.boot_id or ready["state"] != "ready":
        raise RuntimeError("调用不属于当前就绪实例")
    directory = root / "run/self-deploy"
    path = directory / f"{identity}.json"
    request = {"targetCommit": commit, "stop": stop.model_dump(), "root": str(root),
               "runtimeEnv": policy["runtime_env"], "mise": current["AKASHIC_MISE"],
               "notifySession": session, "noExtra": True, "backup": False,
               "unit": f"akashic-publish@{identity}.service"}
    with release_lock(root / "run/admission.lock"):
        if path.exists():
            saved = read_json(protected(path))
            if saved["request"] != request or saved["status"] not in {"accepted", "running", "active"}:
                raise RuntimeError("同一调用已有不同或已失败的任务，不自动重放")
        else:
            for existing in directory.glob("*.json"):
                if read_json(protected(existing))["status"] in {"accepted", "running"}:
                    raise RuntimeError("已有发布任务；请先查询其结果")
            saved = {"request": request, "status": "accepted", "phase": "accepted", "submittedAt": _utc_now_iso()}
            write_json(path, saved, mode=0o640)
            try:
                subprocess.run(["/usr/bin/systemctl", "start", "--no-block", request["unit"]], check=True,
                               capture_output=True, text=True, timeout=15)
            except Exception:
                saved.update(status="failed", phase="failed", error="宿主未启动发布任务", notice="not-accepted")
                write_json(path, saved, mode=0o640)
                raise
    return {"requestId": identity, "status": saved["status"], "unit": request["unit"],
            "targetCommit": commit, "notifySession": session}


def dispatch(policy: dict[str, Any], message: dict[str, Any]) -> dict[str, Any]:
    if message.get("action") in ("prepare-pr", "preparation-status"):
        from scripts.akashic_release.promotion import request
        return request(policy, message)
    if message == {"action": "check"}:
        return {"authority": "administrator", "preparedReleasesOnly": True}
    if set(message) == {"action", "commit"} and message["action"] == "eligible":
        approval(policy, message["commit"])
        return {"approved": True}
    if message.get("action") == "submit":
        return _submit(policy, message)
    if set(message) == {"action", "requestId"} and message["action"] == "status":
        identity = _identity(message["requestId"])
        record = json.loads(protected(Path(policy["root"]) / "run/self-deploy" / f"{identity}.json").read_text())
        record["service"] = subprocess.run(
            ["/usr/bin/systemctl", "show", record["request"]["unit"], "-p", "ActiveState", "-p", "Result"],
            check=False, text=True, capture_output=True, timeout=5,
        ).stdout.strip()
        return record
    raise ValueError("未知发布操作")


def serve() -> None:
    if os.geteuid() != 0:
        raise PermissionError("发布入口只能由系统 socket 单元启动")
    with socket.socket(fileno=os.dup(0)) as connection:
        connection.settimeout(10)
        try:
            policy = read_policy()
            _, uid, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != policy["runtime_uid"]:
                raise PermissionError("调用用户不属于该实例")
            with connection.makefile("rb") as stream:
                raw = stream.readline(_LIMIT + 1)
            if not raw.endswith(b"\n") or len(raw) > _LIMIT:
                raise ValueError("发布请求不完整或过大")
            message = json.loads(raw)
            if not isinstance(message, dict):
                raise ValueError("发布请求必须是对象")
            result = {"ok": True, "result": dispatch(policy, message)}
        except Exception as error:
            result = {"ok": False, "error": str(error)}
        connection.sendall(json.dumps(result, ensure_ascii=False).encode() + b"\n")


def worker(identity: str) -> None:
    from scripts.akashic_release.activate import activate_release
    from scripts.akashic_release.doctor import read_environment
    from scripts.akashic_release.extra import ExtraOptions
    from scripts.akashic_release.manifest import read_json, release_lock, write_json
    from scripts.akashic_release.model import ReleasePaths
    from scripts.akashic_release.publisher_mode import run, state_action
    from scripts.akashic_release.self_deploy import _utc_now_iso, notify_failure

    if os.geteuid() != 0:
        raise PermissionError("发布 worker 必须由系统单元启动")
    policy = read_policy()
    root = Path(policy["root"])
    path = protected(root / "run/self-deploy" / f"{_identity(identity)}.json")
    with release_lock(root / "run/release.lock"):
        record = read_json(path)
        if record["status"] != "accepted":
            raise RuntimeError("任务未接纳或已经执行")
        record.update(status="running", phase="preparing", recordPath=str(path), startedAt=_utc_now_iso())
        write_json(path, record, mode=0o640)
        try:
            request = record["request"]
            release = approval(policy, request["targetCommit"])
            environment = Path(policy["runtime_env"])
            current = read_environment(environment)
            approval(policy, current["AKASHIC_RUNTIME_COMMIT"])
            if request["root"] != str(root) or request["runtimeEnv"] != str(environment):
                raise ValueError("发布任务不属于固定实例")
            os.environ["AKASHIC_PUBLISHER_POLICY"] = str(POLICY)
            ready = state_action("read-object", {"path": str(Path(current["AKASHIC_WORKSPACE"]) / ".runtime-ready.json")})
            if ready["bootId"] != request["stop"]["boot_id"]:
                raise RuntimeError("原 boot 已被替换")
            paths = ReleasePaths(root, state_root=Path(policy["state"]))
            status = activate_release(paths=paths, manifest_path=root / "releases" / f"{request['targetCommit']}.json",
                                      environment_file=environment, mise=Path(current["AKASHIC_MISE"]),
                                      run=run, self_deploy=record, extra=ExtraOptions(enabled=False))
            record.update(status="active", phase="active", result={"status": status, "commit": request["targetCommit"],
                          "imageId": release["imageId"], "degradedExternal": read_json(root / "activation/active.json")["runtimeCheck"]["degraded_external"]})
        except Exception as error:
            record.update(status="failed", phase="failed", error=f"{type(error).__name__}: {error}")
            write_json(path, record, mode=0o640)
            record["notice"] = notify_failure(record)
            raise
        finally:
            record["finishedAt"] = _utc_now_iso()
            write_json(path, record, mode=0o640)


def main() -> None:
    sys.path.insert(0, str(protected(Path(__file__).resolve().parents[2])))
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("serve", "worker", "prepare-worker"))
    parser.add_argument("identity", nargs="?")
    args = parser.parse_args()
    if args.action == "serve":
        os.environ["AKASHIC_PUBLISHER_POLICY"] = str(POLICY)
        serve()
    elif args.action == "prepare-worker":
        from scripts.akashic_release.promotion import worker as prepare_worker
        prepare_worker(_identity(args.identity))
    else:
        worker(_identity(args.identity))


if __name__ == "__main__":
    main()
