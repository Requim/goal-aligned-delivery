# 目标对齐交付

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

## 评价与改进

具备文件与 Python 能力、允许持久化且非评测的标准/严格调用，开始读取相关已验证经验，收尾记录实际效果、
工具/子代理/Token 开销和任务执行、流程维护、学习验证三类时耗及来源。
私有评价在原 workspace 的 `work/task-evaluations`；候选、周期、源码快照、journal
和版本在 `work/skill-learning/goal-aligned-delivery`。`--data-dir` 共享根只保存
去标识指标、类别与通用经验；续做必须传原 --workspace。
共享目录优先级为 `--data-dir > GAD_DATA_DIR > OS user data`；Windows 使用
LOCALAPPDATA，macOS 使用 Library/Application Support，Linux 使用 XDG_DATA_HOME
或 .local/share，再加 goal-aligned-delivery。环境路径须绝对且非空。发现旧
Documents/Codex 目录且未明确选择时返回 pending，不迁移、不覆盖；显式 --data-dir
可继续使用原目录。--no-write 在路径解析前返回，不创建目录。

每轮600秒、一个候选、前台完成，到时保留继续；不启动后台/自动化，不自动 commit/push。
功能候选需 baseline 失败复现与候选两次独立通过；效率候选需三对独立运行、
质量无退步且指定实测成本 median 下降。执行规则改动做八类回归，局部参考也加
lightweight/no-write/resume；候选还需独立边界评审。权限、学习机制、工具与旧判据
不可自动修改，证据不足只保留候选。GAD_EVALUATION/GAD_LEARNING_DISABLED 禁止学习。

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
