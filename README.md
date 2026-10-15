# 目标对齐交付

当前版本：**v1.0.0** · 更新日期：2026-10-15。各版本内容见[版本与更新记录](#版本与更新记录)。

`goal-aligned-delivery` 是一个面向 AI 助手的技能包，用于将需求转化为可验证的交付结果。
适用于文档写作、资料研究、数据分析、方案设计、开发、运维和技能制作。

它根据任务风险选择轻量、标准或严格的执行方式，不把普通请求变成复杂工程项目。
完成标准来自用户目标与可复核证据，而不是操作数量或助手的自述。
核心只需宿主读取指令；只有对话时使用明确未持久化的交接摘要。有获准工作区和
Python 时，多步骤任务复用当前权威计划/进度；新进度在任务工作区
`work/task-progress/<task-id>.md` 中，以单 JSON 权威块生成中文视图，
随执行、验证、阻塞和需求变化以 revision/CAS 更新。续做先读记录并核对现场；
旧 Markdown 仅显式 upgrade 且保留原文。轻量任务不强制建档、评价或学习。
禁止落盘时，任务记录、共享经验、候选与自动学习全部禁写，只回复可恢复摘要。

## 核心流程

1. 明确目标、范围、约束和验收条件。
2. 核对当前材料、事实和前置条件。
3. 在授权范围内执行，区分事实、推断与未知。
4. 先确定有效判据，再按可验证单元交付。
5. 用证据诊断问题，保留成功结果与恢复边界。
6. 分别复核目标完成情况和交付质量。
7. 区分通过、失败、阻塞和未验证，受控收尾。

## 文件结构

```text
SKILL.md                          技能入口与执行原则
agents/openai.yaml                可选 OpenAI 宿主展示元数据
references/capabilities.md        三档宿主能力与真实降级
references/adapters.md            版本化执行器接口与验证状态
references/execution.md           执行、复核和恢复
references/progress.md            进度文件、状态更新与中断续做
references/contracts.md           交付契约、证据格式与检查器用法
references/behavior-cases.md      行为验收案例
references/authority.md           权限、多 skill 分工与冲突处理（受保护）
references/learning.md            评价、单候选改进、版本与恢复（受保护）
references/principles.md          原理映射与能力边界
scripts/delivery_io.py            原子 IO、结构化错误与跨进程锁
scripts/progress.py               init/update/check/upgrade 进度工具
scripts/regress.py                offline/live run 与 compare 行为回归
scripts/executors.py              内置 Codex/Claude Code 协议适配器
scripts/learning.py               record/summarize/stage/apply/rollback
scripts/check_delivery.py         可选证据检查器，保留 v1/v2 兼容
scripts/test_*.py                 工具与兼容性测试
cases/<id>/                      八类固定请求、原始输入与独立评审 oracle
```

以 [SKILL.md](SKILL.md) 为入口，按需阅读参考。实际命令见
[进度记录](references/progress.md)、[行为验收](references/behavior-cases.md)、
[受控学习](references/learning.md) 和 [可选证据检查器](references/contracts.md)。
工具与字段名保留英文，正文按中文维护，不复制双语规则。

## 任务复盘与经验积累

这部分用于记录任务做得怎样、哪里需要返工、花了多少时间和调用开销，
让后续任务复用经过验证的经验。规则调整先形成候选，验证通过后才在本地生效，并保留回退能力。

具备文件与 Python 能力、允许保存记录且处于非评测环境时，标准/严格任务按以下流程执行。
轻量任务不强制记录；用户禁止落盘时，不写任务记录、共享经验或学习状态。

1. **开始前查经验**：读取与当前任务相关的已验证经验，避免重复出现已知问题。
2. **结束后记结果**：记录验收结果、失败原因、用户纠正、重试与恢复情况，以及工具、子代理和 Token 开销。
   耗时分为任务执行、流程维护、学习验证三类，并注明数据来源；无法取得的指标使用 `null / unavailable`。
3. **有依据再改规则**：收到明确的用户纠错，或同类问题在两个独立任务中出现时，生成一个候选改动。
   用户反馈、待验证推断和已验证经验分别保存，只有验证通过的经验才能驱动规则生效。

### 记录保存在哪里

| 内容 | 保存位置 |
| --- | --- |
| 任务详细复盘、原始反馈和指标来源 | 原任务工作区中的 `work/task-evaluations` |
| 候选改动、验证周期、源码快照、操作日志（`journal`）和本地版本 | 原任务工作区中的 `work/skill-learning/goal-aligned-delivery` |
| 可跨项目复用的去标识指标、问题类别和通用经验 | `--data-dir` 指定的共享目录 |

续做候选、应用改动或回退时，`--workspace` 必须指向原任务工作区。共享目录按
`--data-dir > GAD_DATA_DIR > OS user data` 选择；各平台默认路径和参数约束见
[评价与受控学习](references/learning.md#位置与隐私)。
发现旧 `Documents/Codex/skill-data/goal-aligned-delivery` 数据目录且未明确选择时返回 `pending`，保留原数据；
可显式传 `--data-dir` 继续使用原目录。`--no-write` 在路径解析前返回，不创建目录。

### 什么条件下可以改规则

| 修改目的 | 生效前的验证要求 |
| --- | --- |
| 修正行为错误 | 先复现旧版失败，再确认候选连续两次独立通过，且必需回归无退步 |
| 降低时间或调用开销 | 至少三组成对运行，质量无退步，指定实测开销的中位数下降 |

执行规则变化须运行全部八类回归；局部参考改动也须验证受影响案例，并补跑
`lightweight / no-write / resume`。候选还须通过独立边界评审，离线检查、模拟或证据不足时不能自动生效。

每轮自动维护最多 **10 分钟（600 秒）**，只处理一个候选，在前台执行；到时保存进展，后续再续做。
自动改动范围限普通正文、普通参考和新增案例；权限、学习机制、工具代码及旧判据等受保护内容不可自动修改。
不启动后台任务或自动化，也不自动 `commit / push`。设置 `GAD_EVALUATION` 或 `GAD_LEARNING_DISABLED` 时禁止学习。

## 运行测试

检查器和测试使用 Python 标准库，不需要额外安装第三方依赖。

```bash
python -B -m unittest discover -s scripts -p "test_*.py" -v
```

测试使用临时目录；其中旧真实报告只读兼容检查需要原验收工作区的
`../live-baseline/report.json`。单独分发技能包而没有该历史证据时，不应伪造它以求全绿，
应说明相应验证缺失；工具使用不依赖历史报告。

行为核验另运行：

```text
python -B scripts/regress.py run --mode offline --skill . --output <workspace>/work/regression-offline
python -B scripts/regress.py run --mode live --skill . --output <workspace>/work/regression-live --timeout 180 --budget-seconds 600
python -B scripts/regress.py compare <baseline-report.json> <candidate-report.json>
```

live 通过 `--backend codex|claude` 选择内置协议，默认 codex；--executor 仅指定该协议的
可执行文件，--model 选择用户授权模型，不改全局配置。Claude Code 目前仅协议测试，
真实模型行为 unverified；详见 [适配器](references/adapters.md)。自动晋升另需真实证据与独立评审。
Codex 具有协议与历史证据覆盖；具体版本的真实状态以绑定 source/change 摘要的外部验收报告为准，
不从单次或历史通过推断未来版本普遍兼容。Claude live 缺省或空白 --model 会在 CLI 探测和输出创建前拒绝。
offline 只查格式，始终 unverified；单测中的模拟执行器通过只证明协议。
八类自动化与保留的18项人工清单覆盖范围不同，不能仅因案例存在就说行为已验收。

## 能力边界

检查器核对证据覆盖、文件摘要、任务身份、环境、状态和时间约束，
不证明日志真实，不自动判断业务结果正确，也不替代人工审核、权限控制或发布审批。
合成夹具测试通过不代表任何真实项目已经验收。progress check 明确
`semantically_certified=false`；regress 报告必须经 load_report 完整校验原始执行、
身份、实际产物与独立评审，不能靠摘要自评。旧报告未绑定 elapsed 为 unavailable，
不能据此证明提效；新 report elapsed 也不是完整运行墙钟，详见行为/学习参考。

学习应用/回退采用快照、日志、CAS 与版本链，保留用户新修改；路径保护和自然语言规则
不是宿主权限沙箱。Windows 与其他平台的实际锁、崩溃、子进程清理仍按目标环境核验。

本仓库保存技能本身，不包含使用该技能处理过的项目代码、会话记录或凭据。
演练领域 skill 保存为 `SKILL.md.fixture`，仅在隔离评测工作区物化为 SKILL.md；
正式包内只保留入口可发现，不把演练 skill 暴露为用户正式技能。

## 版本与更新记录

从 `v1.0.0` 开始维护版本号；此前发布未编号，按实际 Git 提交追溯。
版本号在此记录，不改变进度 JSON、回归报告或适配器各自的格式版本。

| 版本 | 日期 | 对应内容 |
| --- | --- | --- |
| **v1.0.0（当前）** | 2026-10-15 | 首次引入版本编号与更新记录；将“评价与改进”改为“任务复盘与经验积累”，说明记录内容、保存位置和规则生效条件 |
| 历史未编号（`0b0bc12`） | 2026-10-15 | 通用 AI 指令核心与 Codex / Claude Code 执行适配；可靠进度工具、八类行为回归、外部操作恢复和受控学习 |
| 历史未编号（`4a0ad5f`） | 2026-10-15 | 增加任务进度记录与中断续做机制 |
| 历史未编号（`848d0cc`） | 2026-09-30 | 首次发布技能入口、参考文档与交付证据检查器 |
