"""Long-term extraction criteria adapted from upstream engine 47e0d7cc (MIT).

The current evidence-bearing JSON schema and user-only event channel remain
owned by learning.py; these criteria do not authorize treating assistant text
or a compaction summary as user evidence.
"""

LONG_TERM_RULES = """
作者最后版的长期档案筛选标准（用于完整对话批次）：
- 默认不提取。把信息放到六个月后的新对话，仍有用才作为长期档案。
- profile 是用户客观事实、持有物、重要决定或长期状态；category 使用
  personal_fact / purchase / decision / status。每件具体的事独立一条。
- preference 是用户希望怎样被服务、讲解、推荐的稳定偏好。
- procedure 是用户要求未来相似任务遵循的步骤或工具约束。只有方向性倾向时用 preference。
- 先检查是否为元讨论或教学举例；助手给出的例子不是用户要求。
- 用户必须有直接原话锚点。用户提问、追问、记忆测试句不能反推事实；
  没有反驳助手不等于认可助手建议，也不等于要求长期执行。
- 当前任务的临时策略、纯状态汇报、通用知识问答、助手解释和工具输出，
  不能作为用户长期档案。不能由“今晚想吃日料”推断长期喜欢日料。
- profile 不收工程安装/配置操作、项目架构讨论或纯意见。
- 情绪权重：普通技术与事实为 0；明确喜恶或强烈在意时保守给 3–9。
- summary 独立成句、只表达一个可核对事实，语气不强于用户原话，
  不补助手推断，不用“今天/这次”代替已知日期。
事件是独立通道：只记录用户明确陈述的个人事件，注明有证据的发生时间；
不能把助手声称完成的动作写成用户的经历。
"""
