# Akashic

Akashic 是一个可以在本机运行的 AI 助手，支持跨会话回忆，也能根据用户的嘱托主动发消息。本仓库由 Pengju Wang（namei32）维护，基于 [Akashic Agent](https://github.com/kachofugetsu09/akashic-agent) 开发，个人主线为 `main`。[MIT 许可证](./LICENSE)

## 这个版本做过哪些改进

- **主动反馈**。在上游 Watch 的基础上，给 EventMail 增加到期告警的只读查询，让 Watch 按作用域分流事件、统一调度主动联系，并让权限检查识别已安装的 Watch 发行版。[事件分流](https://github.com/namei32/akashic/commit/8a991fd893a5408159d541b6ff29b1d532e2774c) · [调度收口](https://github.com/namei32/akashic/commit/babf72865f5a8e1547d1ff564808e9aaa075e06e) · [发送权限](https://github.com/namei32/akashic/commit/fb9b256ee568ac3923529ac1693ca0bd6ef0c2c4)
- **跨会话记忆**。为 Akasha 实现按需回读。例如用户在新会话里问“上次定的周报时间是什么”，召回先给出历史消息的时间、短预览和引用，模型需要核实细节时，再按引用读取当前作用域允许访问的原文。已在窗口里出现的消息会去重。[实现提交](https://github.com/namei32/akashic/commit/7a3f3134821b7b38aef50d0e56fa699e80cc731d) · [设计与回放结果](./docs/decisions/0141-akasha-recall-injects-an-index.md)
- **WSL 部署与恢复**。为原生 WSL 安装增加自更新流程，将部署结果送回发起会话，并在新版本启动失败时根据发布记录回滚。[原生部署](https://github.com/namei32/akashic/commit/e970e0b0fa6987aa6d9e19c4eb33fd45edc581e9) · [结果回传](https://github.com/namei32/akashic/commit/997cbbf7613091c9339362e4d87aefc9c0bc51e9) · [失败回滚](https://github.com/namei32/akashic/commit/fbeb83603772dae66af05560572b447aaab85e28)

上述提交可查看个人实现时的完整改动。当前版本将 Watch 和 Akasha 等扩展独立分发，插件来源见下文。

## 安装

### 从源码运行本仓库（Linux 或 WSL2）

需要 Git、Python 3.12+、Node.js 20+ 和 npm。

```bash
git clone --branch main https://github.com/namei32/akashic.git
cd akashic
./start
```

首次启动会准备运行环境并引导选择功能。装好后浏览器会打开登录页面。按 Ctrl+C 停止。

### 体验上游 Docker 发行版

以下命令下载上游发布的镜像配置。要运行本仓库的个人主线，请使用上面的源码安装方式。

先安装 [Docker Desktop](https://docs.docker.com/get-docker/)，并打开它。然后在终端里依次运行：

```bash
mkdir akashic && cd akashic
curl -fsSLO https://github.com/kachofugetsu09/akashic-agent/releases/latest/download/compose.yaml
docker compose up -d --wait
docker compose exec akashic python main.py web-login
```

第一次启动只装核心，大约 1 分钟，主要是下载镜像；期间终端只显示等待；想看进度，另开一个终端运行 `docker compose logs -f`。其余功能在网页引导里挑选安装。最后一条命令会打印一个登录链接，在浏览器里打开它就能进入。

如果报错 `address already in use`，说明 2236 端口被别的程序占了：在同一目录创建 `.env` 文件，写一行 `AKASHIC_PORT=2237`，再运行一次 `docker compose up -d --wait`。

## 第一次打开

1. **设置密码**：页面会让你设置一个自己的密码，至少 10 个字符。以后在其他设备上登录时使用。
2. **跟着引导走**：
   - **连接模型**（必须）：选一个模型服务，填入它的 API Key：内置 DeepSeek、智谱 GLM 等预设；其他兼容 OpenAI 接口的服务选“自定义 API”，再填地址。
   - **选择功能**（用 Docker 时才有这一步）：列出官方扩展包，每个都写明大小和依赖。默认全部安装，不需要的取消勾选即可。安装要几分钟，页面会显示装到第几个；也可以先关掉引导去聊天。
   - 聊天软件、记忆等后面几步都可以先跳过，以后在“功能设置”里随时开。
3. **开始对话**。

想要更多功能，可以直接对 Akashic 说“帮我装上某某功能”，也可以在“功能设置”→“插件”里安装。

## 插件与扩展

Akashic 把“核心闭环”和“功能扩展”拆开维护：

- **核心仓库（本仓库，Core）**：只保留长程主循环（账本、上下文、压缩、基础文件与 Shell）、Web 宿主与包管理，更新 Core 需要重新部署并重启一次。
- **官方扩展仓库（[akashic-plugins](https://github.com/kachofugetsu09/akashic-plugins)，`extra`）**：浏览器、记忆、值班、子任务、画布、手机端侧等非核心功能都在这里，按单个插件的 `release/<插件名>` 分支发布，Core 镜像默认不内置。
- **安装与热更新**：在网页端“功能设置”→“插件”→“可安装”里可一键安装；也可以让 Akashic 帮忙装，或在容器/终端里用 `akashic pkg` 管理（`list` / `groups` / `install <名|@组>` / `upgrade`）。官方扩展插件与私人插件都支持**热安装与热更新**，改完或升级不需要重启 Core。
- **自建插件**：想让 Akashic 自己扩展新能力时，它会按内置 Skill [`develop-akashic-plugin`](./skills/develop-akashic-plugin/SKILL.md) 写插件源码并热加载。

## 常用操作

| 想做的事 | Docker（在 `akashic` 目录里运行） | 不用 Docker（在源码目录里运行） |
| --- | --- | --- |
| 停止 | `docker compose stop` | 按 Ctrl+C |
| 再次启动 | `docker compose up -d --wait` | `./start` |
| 看日志 | `docker compose logs --tail=200` | 终端里会显示日志文件的位置 |
| 拿登录链接 | `docker compose exec akashic python main.py web-login` | `./start --login` |

**不要运行 `docker compose down -v`**，它会删除你的全部数据。

更新、备份、换端口、从其他设备访问、忘记密码等操作，见[使用手册](./docs/user-guide.md)。

## 出问题了

1. 先看日志（见上表）里最后的报错。
2. 处理后重新运行启动命令。中途失败了，直接再运行一次即可，已完成的步骤不会重做。
3. 还是不行，就带上日志的最后 200 行，到[本仓库的 Issues](https://github.com/namei32/akashic/issues) 提问。分享前请删去日志里的登录链接和凭据。

## 给 AI 助手的安装说明

如果你是正在帮用户安装 Akashic 的 AI 助手，按下面做：

- **上游 Docker 发行版**：按上面 Docker 小节的命令依次运行。
  - `docker compose up -d --wait` 返回 0 就表示已就绪。如果返回非 0，运行 `docker compose logs --tail=200 akashic` 查看原因。
  - 把 `web-login` 打印的链接交给用户。这个链接 2 分钟内有效，只能用一次。
- **源码**：运行 `./start --no-browser --yes`。
  - `--yes` 表示首次安装不提问，只装核心，扩展包留给用户在网页引导里挑选。要一次装全，用 `./start --no-browser --yes --profile full`。
  - 看到 “WebUI 已就绪” 就表示完成。进程会一直运行，需要放到后台。
  - 登录链接用 `./start --login` 获取，交给用户。
- **换端口**：
  - Docker：在 `compose.yaml` 同目录创建 `.env`，写入 `AKASHIC_PORT=2237`。
  - 源码：运行 `./start --port 2237`。
- **不要替用户做的事**：
  - 不要替用户填写 API Key 或密码，这两步留给用户在网页上完成。
  - 不要运行 `docker compose down -v`。

## 文档

| 主题 | 文档 |
| --- | --- |
| 使用手册（更新、备份、远程访问、登录） | [使用手册](./docs/user-guide.md) |
| 官方插件仓库（extra） | [akashic-plugins](https://github.com/kachofugetsu09/akashic-plugins) |
| 插件开发与安装 | [插件能力手册](./docs/design/plugin-v3-capabilities.md)、[`develop-akashic-plugin`](./skills/develop-akashic-plugin/SKILL.md)、[`plugin-system`](./plugins/plugin_update/skills/plugin-system/SKILL.md) |
| 手机接入 | [Android Shell](https://github.com/kachofugetsu09/akashic-android-shell) |
| Python 客户端与协议 | [Python SDK](./sdk/python/README.md)、[协议 schema](./schema/app-server-v2.json) |
| 源码构建镜像、开发与部署 | [启动与部署说明](./docs/design/product-startup.md)、[部署操作手册](./docs/design/operator-deployment.md) |
| 项目需求与开发流程 | [工作手册索引](./docs/INDEX.md) |
