"""Stage the administrator-owned Akashic release control plane."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.akashic_release.approve import seal
from scripts.akashic_release.authority_policy import POLICY, protected
from scripts.akashic_release.doctor import read_environment
from scripts.akashic_release.manifest import atomic_write

ROOT = Path("/var/lib/akashic-publisher")
INSTALL = Path("/opt/akashic-publisher/current")
CONFIG = POLICY.parent
SYSTEMD = Path("/etc/systemd/system")


def _install_text(path: Path, content: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    protected(path.parent)
    if path.exists() or path.is_symlink():
        raise FileExistsError(path)
    atomic_write(path, content, mode=mode)


def _stage_publisher(source: Path, python_environment: Path) -> None:
    shutil.copytree(source, INSTALL / "source", symlinks=True,
                    ignore=shutil.ignore_patterns(".git", "AGENTS.md", ".venv", "node_modules", "__pycache__"))
    shutil.copytree(python_environment.resolve(strict=True), INSTALL / "python", symlinks=True,
                    ignore=shutil.ignore_patterns("__pycache__"))
    for path in (INSTALL / "python").rglob("*.pth"):
        if path.is_symlink() or any(line.startswith("import ") for line in path.read_text().splitlines()):
            raise ValueError(f"发布器 Python 不接受 editable 或可执行 .pth: {path}")
    seal(INSTALL)
    # Publisher dependencies use the administrator's interpreter, never an app-owned target.
    for path in (INSTALL / "python").rglob("*"):
        if path.is_symlink():
            protected(path.resolve(strict=True))


def _configure_bindings(state: Path) -> dict[str, str]:
    bindings = {"state": state}
    runtime = state / "workspace/runtime"
    for name in ("plugin-python-environments", "plugin-resources"):
        bindings[name] = runtime / name
    bindings["egress"] = state / "workload-runtime/host-egress"
    bindings["workload-runtime"] = state / "workload-runtime"
    bindings["plugin-data"] = state / "workspace/plugin-data"
    bindings["plugin-validation"] = runtime / "plugin-validation"
    result: dict[str, str] = {}
    for name, source in bindings.items():
        if source.resolve(strict=True) != source or not source.is_dir():
            raise ValueError(f"发布挂载源缺失或经过软链接: {source}")
        mount = ROOT / "mounts" / name
        mount.mkdir(parents=True)
        _install_text(ROOT / "mounts" / (name + ".source"), str(source) + "\n")
        result[str(source)] = str(mount)
    return result


def _stage_runtime(values: dict[str, str], uid: int, gid: int, controller_image: str) -> None:
    compose = (INSTALL / "source/docker/host-runtime/compose.experiment.yaml").read_text()
    core, controller = compose.split("\n  workload-controller:\n")
    source = '${AKASHIC_EXPERIMENT_ROOT:?AKASHIC_EXPERIMENT_ROOT is required}'
    core = core.replace(f'source: "{source}"', f"source: {ROOT}/mounts/state")
    core = core.replace(
        '      AKASHIC_RELEASE_MANIFEST: /run/akashic-release.json',
        f'      AKASHIC_RELEASE_MANIFEST: /run/akashic-release.json\n      AKASHIC_DEPLOY_STATUS_DIR: "{ROOT}/run/self-deploy"',
    )
    core = core.replace(
        'target: "${AKASHIC_HOST_BRIDGE_DIR:?AKASHIC_HOST_BRIDGE_DIR is required}"',
        'target: "${AKASHIC_HOST_BRIDGE_DIR:?AKASHIC_HOST_BRIDGE_DIR is required}"\n        read_only: true',
    )
    host_runtime = '${AKASHIC_WORKLOAD_RUNTIME_DIR:-${AKASHIC_EXPERIMENT_ROOT}/workload-runtime}'
    core = core.replace(f'source: "{host_runtime}"', f"source: {ROOT}/mounts/workload-runtime")
    controller = controller.replace('image: "${AKASHIC_IMAGE:?AKASHIC_IMAGE is required}"', f"image: {controller_image}")
    controller = controller.replace(f"{source}/workload-controller", str(ROOT / "controller/state"))
    controller = controller.replace(f'source: "{host_runtime}"', f"source: {ROOT}/mounts/workload-runtime")
    for name, source_suffix in (("plugin-data", "plugin-data"), ("plugin-validation", "runtime/plugin-validation")):
        controller = controller.replace(
            f'source: "${{AKASHIC_WORKSPACE:?AKASHIC_WORKSPACE is required}}/{source_suffix}"',
            f"source: {ROOT}/mounts/{name}",
        )
    controller = controller.replace(
        '      - --host-egress-dir\n',
        f"      - --policy\n      - {CONFIG}/workloads.json\n      - --host-egress-dir\n",
    )
    controller = controller.replace(
        "      - type: bind\n        source: /var/run/docker.sock",
        f"      - type: bind\n        source: {CONFIG}/workloads.json\n        target: {CONFIG}/workloads.json\n"
        "        read_only: true\n      - type: bind\n        source: /var/run/docker.sock",
    )
    _install_text(CONFIG / "compose.yaml", core + "\n  workload-controller:\n" + controller)
    _install_text(CONFIG / "services.yaml", (INSTALL / "source/docker/host-runtime/compose.external-services.yaml").read_text())
    _install_text(CONFIG / "local.yaml", "services:\n  akashic-core:\n    environment:\n      OPENCODE_GO_API_KEY: !reset null\n")
    unit_source = INSTALL / "source/docker/host-runtime/systemd"
    for unit in ("akashic-publisher.socket", "akashic-publisher@.service", "akashic-publish@.service"):
        text = (unit_source / unit).read_text().replace("SocketGroup=huashen", f"SocketGroup={gid}")
        _install_text(SYSTEMD / unit, text)
    core_unit = (unit_source / "akashic-core.service").read_text()
    core_lines = []
    for line in core_unit.splitlines():
        if line.startswith(("User=", "Group=", "SupplementaryGroups=", "ExecStartPre=")):
            continue
        line = line.replace("EnvironmentFile=%h/.config/akashic-container/runtime.env", f"EnvironmentFile={ROOT}/runtime.env")
        line = re.sub(r"--file \$\{AKASHIC_RUNTIME_CHECKOUT\}/docker/host-runtime/compose\.experiment\.yaml "
                      r"--file \$\{AKASHIC_RUNTIME_CHECKOUT\}/docker/host-runtime/compose\.external-services\.yaml "
                      r"\$AKASHIC_COMPOSE_ARGS",
                      f"--file {CONFIG}/compose.yaml --file {CONFIG}/services.yaml --file {CONFIG}/local.yaml", line)
        core_lines.append(line)
    _install_text(CONFIG / "akashic-core.service", "\n".join(core_lines) + "\n")
    bridge_unit = (unit_source / "akashic-host-bridge.service").read_text()
    launch = f"{INSTALL}/python/bin/python -I -B {INSTALL}/source/scripts/akashic_release/sandbox.py"
    bridge_unit = bridge_unit.replace("[Service]", "[Service]\nNoNewPrivileges=yes\nUnsetEnvironment=OPENCODE_GO_API_KEY")
    bridge_unit = bridge_unit.replace("User=huashen", f"User={uid}").replace("Group=huashen", f"Group={gid}")
    bridge_unit = bridge_unit.replace("EnvironmentFile=%h/.config/akashic-container/runtime.env", f"EnvironmentFile={ROOT}/runtime.env")
    bridge_unit = bridge_unit.replace("ExecStartPre=/usr/bin/env", f"ExecStartPre={launch} /usr/bin/env")
    bridge_unit = bridge_unit.replace("ExecStart=/usr/bin/env", f"ExecStart={launch} /usr/bin/env")
    _install_text(CONFIG / "akashic-host-bridge.service", bridge_unit)
    values["AKASHIC_CONTROLLER_STATE_ROOT"] = str(ROOT / "controller/state")


def main() -> None:
    parser = argparse.ArgumentParser(description="准备受保护发布器和固定运行配置，不切换运行中的服务")
    parser.add_argument("--source", type=Path, required=True, help="管理员核验后的源代码")
    parser.add_argument("--python", type=Path, required=True, help="管理员构建的非 editable Python venv")
    parser.add_argument("--seed-env", type=Path, required=True, help="管理员审查的运行环境配置")
    parser.add_argument("--workloads", type=Path, required=True, help="管理员审查的工作负载授权")
    parser.add_argument("--controller-image", required=True)
    args = parser.parse_args()
    if os.geteuid() != 0 or not re.fullmatch(r"sha256:[0-9a-f]{64}", args.controller_image):
        raise PermissionError("须由管理员指定已批准 Controller 镜像摘要")
    for path in (ROOT, INSTALL, CONFIG):
        if path.exists():
            raise FileExistsError(f"检测到已有安装，保留现场: {path}")
    # The seed is an administrator input. Never import directly from a live, app-writable runtime.env.
    values = read_environment(protected(args.seed_env))
    uid = int(values["AKASHIC_UID"])
    account = pwd.getpwuid(uid)
    gid = int(values["AKASHIC_GID"])
    if account.pw_gid != gid or uid <= 0:
        raise ValueError("运行账号或组无效")
    for key in ("AKASHIC_MISE", "AKASHIC_HOST_PYTHON_PREFIX", "MISE_DATA_DIR"):
        protected(Path(values[key]))
    if not Path("/usr/bin/bwrap").is_file():
        raise RuntimeError("宿主缺少 Bubblewrap")
    subprocess.run(["/usr/bin/docker", "image", "inspect", args.controller_image],
                   stdout=subprocess.DEVNULL, check=True)
    _stage_publisher(protected(args.source), protected(args.python))
    for name, mode in (("run", 0o755), ("run/self-deploy", 0o2750), ("run/bridge", 0o750),
                       ("runtime/host-executions", 0o700), ("secrets", 0o750),
                       ("activation", 0o755), ("approved", 0o700),
                       ("releases", 0o755), ("controller", 0o700), ("controller/state", 0o700)):
        path = ROOT / name
        path.mkdir(parents=True, exist_ok=True)
        os.chown(path, uid if name in {"run/bridge", "runtime/host-executions", "controller/state"} else 0, gid)
        path.chmod(mode)
    state = Path(values["AKASHIC_EXPERIMENT_ROOT"])
    mounts = _configure_bindings(state)
    _stage_runtime(values, uid, gid, args.controller_image)
    _install_text(CONFIG / "workloads.json", protected(args.workloads).read_text())
    _install_text(POLICY, json.dumps({"version": 1, "runtime_uid": uid, "state": str(state),
                                    "root": str(ROOT), "runtime_env": str(ROOT / "runtime.env"),
                                    "publisher": str(INSTALL), "mounts": mounts,
                                    "controller_state": str(ROOT / "controller/state"),
                                    "origin": "https://github.com/namei32/akashic.git"}) + "\n", 0o600)
    # Docker Compose receives an administrator-owned file and a fixed executable search path.
    for key in ("AKASHIC_COMPOSE_ARGS", "AKASHIC_DEPLOY_WORKER_HELPER", "DOCKER_CONFIG"):
        values.pop(key, None)
    values.update(PATH="/usr/bin:/bin", HOME=str(ROOT), AKASHIC_PUBLISHER_SOCKET="/run/akashic-publisher/control.sock",
                  AKASHIC_DEPLOY_STATUS_DIR=str(ROOT / "run/self-deploy"))
    path = ROOT / "runtime.env"
    _install_text(path, "".join(f"{key}={value}\n" for key, value in sorted(values.items())), 0o640)
    os.chown(path, 0, gid)
    print(json.dumps({"status": "staged", "policy": str(POLICY), "units": str(CONFIG)}))


if __name__ == "__main__":
    main()
