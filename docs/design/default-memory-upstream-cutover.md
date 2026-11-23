# 上游同步与 Default Memory 切换

同步基线为上游 Core `a2d0509c`。Watch 使用上游 extra 包，核对版本为 1.11.0（`04d50e7`），采用上游的定时、后台程序调用和来源脚本。基础组合选用 Default Memory 2.1.2 作为结构化记忆与召回提供者，关闭 Akasha。Watch 使用的 Markdown 档案与记忆整理仍按上游方式运行。

Default Memory 沿用 `memory/default-v2.db`、已有学习状态和回执。升级以当前 ledger 中的 Akasha 作用域策略生成 Default Memory 的固定配置。策略的全局、隔离、禁止学习、禁止召回各自沿用，原 ledger 和 Akasha 图原位保留。正式实例当前的 Default Memory 处于 staging，切换前要核对合资格历史的补学覆盖、暂停与失败积压，再继续使用原插件数据身份。

从仓库根运行 `python -m scripts.default_memory_cutover --workspace <workspace>` 可做只读预检。可选的 `--output <新文件>` 写一份权限 0600 的拟议配置，内含私聊 ID，供审阅。原 channel、recipient 和 session 正好构成上游的单一会话 ID 时，预检才映射联系目标。进后台的工具与脚本权限按上游批准的边界运行。

旧 Wake/Drift 数据、原定时设置、暂停、主动记忆授权和回执仍归原档。旧定时交流窗口与上游的轮询、待看和定点复盘含义不同，预检会标出待核对的旧设置。先在正式数据的一致性副本上审定切换、验证配置和回执，再进行发布。
