# 目标对齐交付

`goal-aligned-delivery` 是一个面向 AI 助手的技能包，用于将需求转化为可验证的交付结果。
适用于文档写作、资料研究、数据分析、方案设计、开发、运维和技能制作。

它根据任务风险选择轻量、标准或严格的执行方式，不把普通请求变成复杂工程项目。
完成标准来自用户目标与可复核证据，而不是操作数量或助手的自述。

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
agents/openai.yaml                界面信息与默认提示
references/execution.md           执行、复核和恢复
references/contracts.md           交付契约、证据格式与检查器用法
references/behavior-cases.md      行为验收案例
references/principles.md          原理映射与能力边界
scripts/check_delivery.py         证据一致性检查器
scripts/test_check_delivery.py    检查器回归测试
```

以 [SKILL.md](SKILL.md) 为入口，按需阅读参考文档。证据检查器的输入、
命令和退出状态详见 [契约与检查器](references/contracts.md)。

## 运行测试

检查器和测试使用 Python 标准库，不需要额外安装第三方依赖。

```bash
python -m unittest discover -s scripts -p "test_*.py" -v
```

## 能力边界

检查器核对证据覆盖、文件摘要、任务身份、环境、状态和时间约束，
不证明日志真实，不自动判断业务结果正确，也不替代人工审核、权限控制或发布审批。
合成夹具测试通过不代表任何真实项目已经验收。

本仓库保存技能本身，不包含使用该技能处理过的项目代码、会话记录或凭据。
