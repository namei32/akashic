---
name: deploy-akashic
description: 用户明确授权更新当前 Akashic 时，通过 Shell 安装外置插件，或给 Docker 和原生 WSL 宿主提交发行版部署任务。
---

# 部署当前 Akashic

本回合负责检查、合并和提交。宿主负责等待回复送达、停止、迁移与启动。
成功接单不等于更新成功；提交之后不再操作，让本回合正常结束。

```text
┌────────────── Agent 当前回合 ──────────────┐
│ 检查 → 合并 → 固定 SHA → Shell submit → 最终回复 │
└──────────────────────┬───────────────────┘
                       ▼
┌────────────── 宿主独立 systemd worker ──────┐
│ 预检 → 核对完成与送达 → 关闭准入并排空 → 更新 │
└───────────────────────────────────────────┘
```

Docker 的前提由部署者一次性准备：安装支持此流程的 Docker 版本及宿主 CLI，
运行用户的 systemd manager 已启动并启用 linger，现有发布器所需的
非交互 sudo、Docker、mise 和 Git 权限已验证。旧 Supervisor 版本需先由
部署者更新一次；Agent 不尝试在缺少停止协议的旧实例内自举。
提交会先保留原调用的送达凭据；`--timeout`（默认 600 秒）包含凭据保留预算，
构建较慢时在最后提交命令显式提高，最大 3600 秒。

## 选择部署入口

先确认用户授权的目标和备份选择。涉及 PR 时核对指定 head 和 CI，只合并明确获授权的 PR，再确定要发布的完整提交。

- 只更新一个与当前 Core 及消费者兼容的外置插件时，走现有 `plugin-install`，固定 Git 提交和 marketplace。
  原生 WSL 用 `akashic-release wsl plugin-install --source <Git来源> --ref <40位SHA> --marketplace <来源名>`。
  记下更新编号，通过 `akashic-release wsl plugin-status <编号>` 核对实际激活的输入与 `active` 状态。
- 原生 WSL 的内置插件或混合更新走宿主发布。先运行 `akashic-release wsl prepare --commit <40位SHA>`，等待制品准备完成。
  混合发布使用绑定当前 Root 的清单，精确列出外置插件 ID、bundle 路径和摘要。把以下提交命令作为最后一次 Shell 操作。

  ```sh
  akashic-release wsl submit --commit <40位SHA> --plan <清单> --inputs <bundle目录>
  ```

  纯内置发布省略 `--plan` 和 `--inputs`。仅在用户指定时加 `--backup`。
  接单后正常回复并结束当前回合。后续回合通过 `akashic-release wsl status <编号>` 查询。
  WSL 宿主需要先由部署者配置，故障后的继续与回退由独立的宿主入口处理。
- Docker 部署按下方步骤提交。

## Docker

1. 从用户请求确定允许合并的 PR 和备份选择；“提 PR”不授权合并或部署。
2. 用 Shell 的 `gh pr view` / `gh pr checks` 核对目标 head、依赖和 CI。
   合并使用 `gh pr merge <number> --squash --match-head-commit <head-sha>`。
   检查失败、head 改变或未解决冲突时停止部署，说明真实原因。
3. 所有获授权 PR 合并后，用 `git ls-remote --exit-code origin refs/heads/main`
   取得唯一完整 40 位 SHA。宿主会准备干净 checkout；不需要在运行目录拉代码。
4. 把下面命令作为**最后一次 Shell 操作，单独执行**：

   ```sh
   akashic-release submit --commit <完整SHA>
   ```

   仅在本次明确要求备份时加 `--backup`；默认不备份。
   CLI 自动读取 Shell 的当前调用身份，不需要填写 session、Turn 或 boot ID。
   如 shell 提前返回运行句柄，先用现有 write_stdin 等待同一个命令退出。
   接单成功应包含 `status: accepted`、`requestId` 和目标 SHA。
5. 正常回复用户：“已提交更新 <SHA>，任务编号 <requestId>。本轮结束后，宿主会等待其他工作收尾再更新；这不是更新成功回执。”
   接单成功后不再查状态、不 load_tools、不运行 Shell、不要求用户继续，也不等待重启。
   必须产生普通最终回复，让 Turn 完整结束。

失败时如实报告，不能把正在执行、submitting 或 failed 说成已接单。
响应不明时不换调用重交；宿主按原调用去重。使用原任务编号查询实际状态。

后续用户明确要求查询时，在新的回合执行：

```sh
akashic-release status <requestId>
journalctl --user -u akashic-deploy-<requestId>.service --no-pager -n 80
```

如果无法启动用户 systemd service，或宿主发布权限未配置，交给部署者修复。
不使用 sleep、nohup、后台 shell、kill、docker restart 或直接 systemctl stop 来更新自己。
宿主等待超时、发送失败或旧 boot 被替换时会拒绝更新；既有消息保留。
