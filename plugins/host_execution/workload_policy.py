"""Administrator-owned workload grants, separate from application/plugin declarations."""
from __future__ import annotations

import json
from pathlib import Path
import stat
from typing import Any

from plugins.host_execution.contract import WorkloadStartRequest


class WorkloadPolicy:
    def __init__(self, path: Path) -> None:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise PermissionError("Workload 政策必须由管理员持有且不可被运行账号改写")
        self._document = json.loads(path.read_text())
        if self._document.get("version") != 1:
            raise ValueError("Workload 政策版本不支持")

    def image(self, image: str) -> None:
        if not any(image in row["images"] for row in self._document["workloads"]):
            raise PermissionError("镜像尚未获宿主批准")

    def grant(self, request: WorkloadStartRequest) -> dict[str, Any]:
        rows = [row for row in self._document["workloads"]
                if (row["plugin"], row["name"], row["mode"]) == (request.plugin_id, request.workload, request.mode)]
        if len(rows) != 1:
            raise PermissionError("该工作负载或验证模式尚未获宿主批准")
        row = rows[0]
        if request.image not in row["images"] or list(request.command) != row["command"]:
            raise PermissionError("工作负载的镜像或启动命令超出批准范围")
        if request.user_namespaces and not row["user_namespaces"]:
            raise PermissionError("该工作负载没有用户命名空间授权")
        if request.host_egress and not row.get("host_egress"):
            raise PermissionError("该工作负载没有宿主出口授权")
        if (not {number for _, number in request.ports}.issubset(row["tcp_ports"])
                or not {number for _, number in request.loopback_ports}.issubset(row["loopback_ports"])
                or not set(request.udp_ports).issubset(row["udp_ports"])):
            raise PermissionError("工作负载端口超出批准范围")
        expected = {(name, item["target"], item["writable"]) for name, item in row["mounts"].items()}
        if set(request.data) != expected:
            raise PermissionError("工作负载挂载声明与宿主批准范围不同")
        return row

    def limits(self, request: WorkloadStartRequest) -> tuple[int, float, int]:
        maximum = self.grant(request)["limits"]
        if any(value > limit for value, limit in zip(request.limits, maximum)):
            raise PermissionError("工作负载资源超过宿主预算")
        memory, cpu, pids = (value or limit for value, limit in zip(request.limits, maximum))
        if memory < 64 or cpu <= 0 or pids < 16:
            raise ValueError("宿主资源预算必须有界")
        return int(memory), float(cpu), int(pids)

    def mounts(self, request: WorkloadStartRequest) -> tuple[tuple[str, str, bool], ...]:
        row = self.grant(request)
        # Sources are administrator-pinned bind mounts, not caller-writable subdirectories.
        mounts = [(row["mounts"][name]["source"], target, writable) for name, target, writable in request.data]
        if request.host_egress:
            mounts.append((row["host_egress"], "/run/akashic/host-egress", False))
        return tuple(mounts)
