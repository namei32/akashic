"""Prepare a GitHub PR release on behalf of the runtime, under a fixed host policy."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile

from scripts.akashic_release.authority_policy import approval, protected, read_policy
from scripts.akashic_release.manifest import read_json, release_lock, write_json

_CONFIG = Path("/etc/akashic-publisher/promotion.json")
_SHA = re.compile(r"[0-9a-f]{40}")
_ID = re.compile(r"[0-9a-f]{32}")
_BUILD_WORKFLOW = "wsl-release-candidate.yml"
_BLOCKED = (".github/", "scripts/akashic_release/", "scripts/akashic-release", "docker/",
            "mise.toml", "mise.lock", "plugins/host_execution/controller", "plugins/host_execution/workload_policy")
_MAX_BYTES = 4 * 1024 ** 3


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Credentials are confined to api.github.com.


_DIRECT = build_opener(_NoRedirect())


def _b64(data: bytes) -> bytes:
    return base64.urlsafe_b64encode(data).rstrip(b"=")


class GitHub:
    def __init__(self, policy: dict[str, Any], configuration: dict[str, Any]) -> None:
        match = re.fullmatch(r"https://github\.com/([\w.-]+/[\w.-]+)\.git", policy["origin"])
        if match is None:
            raise ValueError("自动发布需要管理员绑定 GitHub 仓库")
        self.repo = match[1]
        self.token = ""
        now = int(time.time())
        claims = {"iat": now - 30, "exp": now + 480, "iss": str(configuration["app_id"])}
        signing = b"eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9." + _b64(json.dumps(claims).encode())
        key = protected(Path(configuration["private_key"]))
        if key.stat().st_mode & 0o077:
            raise PermissionError("GitHub App 私钥必须仅管理员可读")
        signature = subprocess.run(["/usr/bin/openssl", "dgst", "-sha256", "-sign", str(key)],
                                   input=signing, capture_output=True, check=True, timeout=10).stdout
        self.token = (signing + b"." + _b64(signature)).decode()
        installation = int(configuration["installation_id"])
        self.token = self.api(f"/app/installations/{installation}/access_tokens", method="POST", data={
            "repositories": [self.repo.split("/")[1]],
            "permissions": {"contents": "write", "pull_requests": "read", "actions": "read"},
        }, absolute=True)["token"]

    def request(self, path: str, method: str = "GET", data: dict[str, Any] | None = None) -> Request:
        return Request("https://api.github.com" + path,
                       data=json.dumps(data).encode() if data is not None else None, method=method,
                       headers={"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                                "Content-Type": "application/json"})

    def api(self, path: str, *, method: str = "GET", data: dict[str, Any] | None = None,
            absolute: bool = False) -> Any:
        endpoint = path if absolute else f"/repos/{self.repo}{path}"
        try:
            with _DIRECT.open(self.request(endpoint, method, data), timeout=35) as response:
                return json.load(response)
        except HTTPError as error:
            raise RuntimeError(f"GitHub 请求失败 HTTP {error.code} ({method} {path.split('?')[0]})") from error

    def download(self, artifact: dict[str, Any], path: Path) -> None:
        digest = artifact.get("digest", "")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest) or not 0 < artifact["size_in_bytes"] <= _MAX_BYTES:
            raise ValueError("制品缺少有效摘要或超过大小限制")
        endpoint = f"/repos/{self.repo}/actions/artifacts/{int(artifact['id'])}/zip"
        try:
            with _DIRECT.open(self.request(endpoint), timeout=35):
                raise RuntimeError("GitHub 未返回制品下载地址")
        except HTTPError as error:
            target = error.headers.get("Location", "")
            if error.code != 302 or urlsplit(target).scheme != "https":
                raise RuntimeError(f"GitHub 制品下载不可用 HTTP {error.code}") from error
        h = hashlib.sha256()
        size = 0
        # A signed storage URL carries its own short-lived authorization.
        with _DIRECT.open(Request(target), timeout=120) as response, path.open("xb") as out:
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > _MAX_BYTES:
                    raise ValueError("制品下载超出大小限制")
                h.update(chunk)
                out.write(chunk)
        if "sha256:" + h.hexdigest() != digest:
            raise ValueError("制品下载摘要不一致")


def _config() -> dict[str, Any]:
    configuration = read_json(protected(_CONFIG))
    if not re.fullmatch(r"[0-9a-f]{64}", configuration["workflow_sha256"]):
        raise ValueError("管理员必须固定构建工作流的内容摘要")
    return configuration


def _runs(github: GitHub, workflow: str, commit: str, event: str) -> list[dict[str, Any]]:
    query = urlencode({"event": event, "head_sha": commit, "per_page": 20})
    return github.api(f"/actions/workflows/{workflow}/runs?{query}")["workflow_runs"]


def _merge(github: GitHub, number: int, head: str) -> str:
    pr = github.api(f"/pulls/{number}")
    if (pr["head"]["sha"] != head or pr["base"]["ref"] != "main"
            or pr["base"]["repo"]["full_name"] != github.repo
            or (pr["head"].get("repo") or {}).get("full_name") != github.repo or pr["draft"]):
        raise ValueError("PR 的仓库、目标或固定 head 不符")
    if not 0 < pr["changed_files"] <= 300:
        raise ValueError("PR 文件数超出自动合并范围")
    paths = [item for page in range(1, (pr["changed_files"] + 99) // 100 + 1)
             for item in github.api(f"/pulls/{number}/files?per_page=100&page={page}")]
    if len(paths) != pr["changed_files"]:
        raise ValueError("无法核对完整 PR 文件清单")
    for row in paths:
        for name in (row["filename"], row.get("previous_filename", "")):
            if name != "docker/host-runtime/systemd/runtime.env.example" and name.startswith(_BLOCKED):
                raise PermissionError(f"控制面改动需管理员处理: {name}")
    if pr["merged"]:
        return pr["merge_commit_sha"]
    if pr["state"] != "open" or pr["mergeable_state"] != "clean":
        raise ValueError("PR 尚未达到 GitHub 可合并状态")
    for name in ("ci.yml", "plugin-boundary.yml"):
        runs = _runs(github, name, head, "pull_request")
        if (not runs or runs[0]["conclusion"] != "success"
                or not any(item["number"] == number for item in runs[0]["pull_requests"])):
            raise RuntimeError(f"PR 的 {name} 未通过")
    result = github.api(f"/pulls/{number}/merge", method="PUT", data={
        "sha": head, "merge_method": "merge",
        "commit_title": f"合并（Akashic）：合入已通过检查的应用改动（#{number}）",
    })
    if result.get("merged") is not True:
        raise RuntimeError("GitHub 未确认合并")
    return result["sha"]


def _artifact(github: GitHub, commit: str, configuration: dict[str, Any]) -> dict[str, Any]:
    code = github.api(f"/contents/.github/workflows/{_BUILD_WORKFLOW}?ref={commit}")
    if hashlib.sha256(base64.b64decode(code["content"])).hexdigest() != configuration["workflow_sha256"]:
        raise PermissionError("目标提交的构建工作流与宿主固定策略不符")
    deadline = time.monotonic() + 2400
    while time.monotonic() < deadline:
        ci = _runs(github, "ci.yml", commit, "push")
        build = _runs(github, _BUILD_WORKFLOW, commit, "push")
        for runs in (ci, build):
            if runs and runs[0]["status"] == "completed" and runs[0]["conclusion"] != "success":
                raise RuntimeError("合并提交的 CI 或构建失败")
        if ci and ci[0]["conclusion"] == "success" and build and build[0]["conclusion"] == "success":
            run = build[0]
            if (run["head_branch"] != "main" or run["head_sha"] != commit or run["head_repository"]["full_name"] != github.repo):
                raise PermissionError("构建运行不属于该仓库 main 的固定提交")
            listing = github.api(f"/actions/runs/{int(run['id'])}/artifacts")["artifacts"]
            matches = [a for a in listing if a["name"] == "wsl-release-" + commit and not a["expired"]]
            if len(matches) != 1:
                raise RuntimeError("构建运行未产生唯一正式制品")
            return matches[0]
        time.sleep(12)
    raise TimeoutError("等待构建和合并后 CI 超时")


def _import(github: GitHub, artifact: dict[str, Any], root: Path, commit: str) -> None:
    with tempfile.TemporaryDirectory(dir=root) as temporary:
        stage = Path(temporary)
        github.download(artifact, stage / "artifact.zip")
        with zipfile.ZipFile(stage / "artifact.zip") as archive:
            members = archive.infolist()
            if (sorted(item.filename for item in members) != ["bridge.tar.gz", "core.tar.gz"]
                    or sum(item.file_size for item in members) > _MAX_BYTES):
                raise ValueError("制品包内容不符")
            for member in members:
                with archive.open(member) as stream, (stage / member.filename).open("xb") as out:
                    shutil.copyfileobj(stream, out, 1024 * 1024)
        with tarfile.open(stage / "bridge.tar.gz") as archive:
            total = 0
            for entry in archive:
                path = Path(entry.name)
                allowed = (path == Path("releases") / f"{commit}.json"
                           or path.is_relative_to(Path("bridge-venvs") / commit))
                total += entry.size
                if not allowed or ".." in path.parts or not (entry.isfile() or entry.isdir()) or total > _MAX_BYTES:
                    raise ValueError("Bridge 制品含未批准的归档条目")
                archive.extract(entry, stage, filter="data")
        release = read_json(stage / "releases" / f"{commit}.json")
        if release.get("sourceCommit") != commit:
            raise ValueError("制品提交不符")
        subprocess.run(["/usr/bin/docker", "load", "--input", str(stage / "core.tar.gz")],
                       check=True, timeout=300)
        subprocess.run([sys.executable, "-I", "-B", str(Path(__file__).with_name("approve.py")),
                        "--commit", commit, "--prepared-root", str(stage)], check=True, timeout=180)


def request(policy: dict[str, Any], message: dict[str, Any]) -> dict[str, Any]:
    _config()
    root = Path(policy["root"]) / "preparations"
    if set(message) == {"action", "id"} and message["action"] == "preparation-status":
        identity = message["id"]
        if not isinstance(identity, str) or not _ID.fullmatch(identity):
            raise ValueError("准备任务编号无效")
        return read_json(protected(root / f"{identity}.json"))
    if set(message) != {"action", "number", "head"} or message["action"] != "prepare-pr":
        raise ValueError("准备入口只接受 PR 编号和固定 head")
    number, head = message["number"], message["head"]
    if type(number) is not int or not 0 < number < 2**31 or not isinstance(head, str) or not _SHA.fullmatch(head):
        raise ValueError("PR 编号或完整 head 无效")
    identity = hashlib.sha256(f"{number}:{head}".encode()).hexdigest()[:32]
    path = root / f"{identity}.json"
    with release_lock(root / "submit.lock"):
        if path.exists():
            return read_json(protected(path))
        if any(read_json(protected(p))["status"] in {"queued", "running"} for p in root.glob("*.json")):
            raise RuntimeError("已有发布准备任务")
        record = {"id": identity, "number": number, "head": head, "status": "queued"}
        write_json(path, record)
        try:
            subprocess.run(["/usr/bin/systemctl", "start", "--no-block", f"akashic-prepare@{identity}.service"],
                           check=True, timeout=15, capture_output=True)
        except Exception:
            write_json(path, {**record, "status": "failed", "error": "准备服务未启动"})
            raise
    return record


def worker(identity: str) -> None:
    if os.geteuid() != 0 or not _ID.fullmatch(identity):
        raise PermissionError("版本准备必须由宿主系统服务执行")
    policy = read_policy()
    root = Path(policy["root"])
    path = protected(root / "preparations" / f"{identity}.json")
    with release_lock(root / "preparations/worker.lock"):
        record = read_json(path)
        if record["status"] != "queued":
            raise RuntimeError("准备任务已处理")
        record.update(status="running", phase="merging")
        write_json(path, record)
        try:
            configuration = _config()
            github = GitHub(policy, configuration)
            commit = _merge(github, record["number"], record["head"])
            if not isinstance(commit, str) or not _SHA.fullmatch(commit):
                raise ValueError("GitHub 未返回有效的合并提交")
            record.update(commit=commit, phase="building")
            write_json(path, record)
            if not (root / "approved" / f"{commit}.json").exists():
                _import(github, _artifact(github, commit, configuration), root, commit)
            approval(policy, commit)
            record.update(status="prepared", phase="approved")
        except Exception as error:
            record.update(status="failed", phase="failed", error=str(error)[:800])
            raise
        finally:
            write_json(path, record)
