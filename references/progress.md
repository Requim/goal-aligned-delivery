# 进度记录与续做

标准/严格任务先复用已有权威计划/状态，不另建第二份计划。轻量任务直接完成并核对；禁止落盘时不创建任何任务或学习文件，只在回复中交接目标、已做、证据、阻塞和下一步，并说明未持久化。

本页工具流程只适用于已授权工作区、可靠文件读写和 Python 标准库实际可用的宿主。只有对话或工具不可用时，继续按同一目标/验收组织交接摘要，明确未持久化、未运行 check、没有 CAS 并发保证；不能把摘要冒充本页工具记录。宿主能力见 [capabilities.md](capabilities.md)。

## 单一权威与位置

没有记录时默认 `work/task-progress/<task-id>.md`；用户指定位置则沿用。首次汇报和收尾给实际路径。`progress.py` 生成中文阅读视图，并在唯一标识 `<!-- goal-delivery-progress:authoritative-json:v1 -->` 后放 fenced JSON 权威块。工具只读取权威 JSON，再完整生成视图；不要手工分别改两处。

已有新版先 `check`。已有旧 Markdown 不自动推断/迁移：核对旧文与现场、整理完整 JSON，再显式 `upgrade`；原文完整保存为同目录 `<record>.pre-upgrade`，不能覆盖不同旧备份。纯讨论记录的下一步仍是讨论，不擅自进入实施。

## 可运行最小 JSON

以下是完整、可通过结构检查的进行中状态，不是完成证据。将代码块作为工作区 `work/state.json` 的内容；它不要求已有产物，因此可直接 `init`。时间和内容应按真实任务替换；示例时间不是本轮验证时间。

```json
{
  "schema_version": 1,
  "revision": 1,
  "task_id": "draft-note",
  "title": "核对并交付说明稿",
  "goal": "按现有材料完成说明稿并核对事实",
  "constraints": ["不新增未经材料支持的事实"],
  "baseline_inputs": [],
  "acceptance_criteria": [
    {"id": "AC-1", "description": "成稿保留材料的关键事实与限制"}
  ],
  "overall_status": "in_progress",
  "units": [
    {
      "id": "U-1",
      "title": "完成说明稿",
      "required": true,
      "status": "in_progress",
      "depends_on": [],
      "artifacts": [],
      "acceptance_results": [
        {"criterion_id": "AC-1", "status": "unverified", "summary": "尚未验收", "evidence": []}
      ]
    }
  ],
  "blockers": [],
  "next_steps": ["读取当前材料并生成说明稿"],
  "decisions": [],
  "operations": [],
  "updated_at": "2026-10-15T09:00:00+08:00"
}
```

## 实际命令

以下命令从 skill 根目录执行，`<workspace>`、`<record>` 和状态路径换成当前任务实际路径。`--root` 必须包含记录与产物；记录在 `work/task-progress`、产物在 `outputs` 时显式传工作区根，不能沿用默认的记录父目录。

```text
python -B scripts/progress.py init <record> --state <workspace>/work/state.json --root <workspace>
python -B scripts/progress.py check <record> --root <workspace>
python -B scripts/progress.py update <record> --state <workspace>/work/state.json --expected-revision 1 --root <workspace>
python -B scripts/progress.py upgrade <legacy-record> --state <workspace>/work/state.json --root <workspace>
```

`init/upgrade` 的输入 revision 必须是1。`update` 先读取当前 revision，提案 JSON 中保留该值并传相同 `--expected-revision`；工具在锁内比较再加一。CAS 不符先重读、合并实际变化，不能覆盖别人更新。`task_id` 不可变。`init` 使用 create-only，不覆盖已有文件或竞争创建。

`init/update/upgrade` 支持 `--no-write`，仅校验并返回 `written=false`，不创建目录、锁、备份或缓存；`check` 本身只读。用户禁止落盘时不要先创建 state.json 来调用校验。stdout 是单个 JSON；退出0成功，1条件未满足，2输入/格式/路径错误。

## schema_version=1 字段

| 字段 | 约定 |
| --- | --- |
| `schema_version, revision` | 固定版本1；revision 为正整数 |
| `task_id, title, goal` | 非空文本，稳定任务身份 |
| `constraints, next_steps` | 文本数组 |
| `baseline_inputs` | `{path, summary}` 数组，记录真实来源与缺口 |
| `acceptance_criteria` | 非空 `{id, description}` 数组，ID 唯一 |
| `overall_status` | `in_progress / awaiting_acceptance / blocked / paused / completed / cancelled` |
| `units` | 非空交付单元数组，见下文 |
| `blockers` | `{id, summary, unit_ids, status}`，status 为 `open / resolved` |
| `decisions` | `{timestamp, summary, reason, affected_units}` 数组 |
| `operations` | 外部操作账目数组，普通内容任务可为空 |
| `updated_at` | 含时区的 ISO-8601 时间 |

单元为 `{id,title,required,status,depends_on,artifacts,acceptance_results}`。status 为 `pending / in_progress / awaiting_verification / completed / blocked / cancelled`；依赖只能引用现有单元，不能重复、自依赖或成环。中文显示为待开始、进行中、待验证、已完成、阻塞、已取消。

`artifacts` 为 `{path,sha256,summary}` 数组；相对 `--root` 的安全文件路径，不接受绝对路径、盘符、ADS、`..` 或解析后逃逸。`acceptance_results` 为 `{criterion_id,status,summary,evidence}` 数组；evidence 是实际观察的文本条目数组，不是旧检查器的证据对象。status 为 `passed / failed / blocked / unverified`；passed 须有证据摘要。

所有验收项至少被一个单元覆盖。完成单元须有真实存在且摘要相符的产物、对应验收且全部通过，其直接依赖也必须 completed，包括非必需依赖；逐层约束保证传递依赖一致。pending/blocked/cancelled 依赖不能支撑下游完成；合法替代路径须先调整依赖并记录决定。尚未完成的独立单元可继续工作，不要求所有单元同步完成。

整体 completed 要求所有必需单元完成、所有验收项至少由一个 completed 单元的 passed 结果满足，无 open blocker，无 executing/unknown 操作。未完成单元的 passed 结果保留在记录中，但不贡献整体完成判定。结构通过仍输出 `semantically_certified=false`，不能证明内容判断真实或正确。

## 随事件更新与恢复

单元产出/核验后、输入更正、阻塞变化及交接/暂停/本轮结束前，更新同一记录；不记录每条工具调用。未验收产物记 awaiting_verification；整体仍缺必需证据记 awaiting_acceptance 或 blocked。局部阻塞但有独立工作则继续；paused/cancelled 要有用户依据。

续做先确认 task_id，读目标、验收、单元、操作账目、阻塞和下一步，再核对最新用户输入、实际文件、输入版本和证据。记录是恢复索引，冲突回到原始材料。变化使相关完成状态失效时，更新基线/决策并撤回受影响项，保留无关证据；update 不会让旧产物的过期哈希阻止撤回，但新提案仍按当前现场严格验证。补齐缺口后解除对应 blocker，从下一未完成项继续。

## 外部操作账目

每项必需字段：

```text
operation_id, target, target_revision, authorization, idempotency_key,
status, evidence, query_observation, authorization_still_valid, attempt, recovery_required
```

`status` 为 `prepared / executing / unknown / succeeded / failed`；evidence 为仅可追加的文本数组。operation_id/target/target_revision/authorization/idempotency_key 创建后不可变，新增操作从 prepared 开始。`attempt` 初始 prepared 可为 null，否则是 `{attempt_id,sequence,started_at}`；每次进入 executing 使用新 ID，sequence 从1递增，并追加执行意图证据。

`query_observation` 为 null 或 `{attempt_id,attempt_sequence,status,observed_at,summary,evidence}`，status 为 `applied / not_applied / query_failed`。可选 `result_observation` 同字段，status 为 `succeeded / failed`。观察必须绑定当前 attempt，时间不早于本次开始；旧 attempt 的查询不能给新尝试背书。

| 当前观察 | 可以做什么 |
| --- | --- |
| 正常新执行返回明确结果，`recovery_required=false` | 用当前 attempt 的 result_observation 记 succeeded/failed |
| 断线或结果不明 | 记 unknown、保留证据，恢复先 query |
| 当前 attempt 查询 applied | 补记 succeeded，不重复外部动作 |
| 当前 attempt 查询 not_applied，且授权仍有效 | 先回 prepared；新 attempt 才可重试，开始时间不早于查询 |
| query_failed | 保持 unknown 并登记 open blocker；不能靠 direct result 绕过 |

导入的 executing 和所有 unknown 都是恢复态，`recovery_required=true`，不能直接借 result_observation 收尾。终态重复记账允许，用于幂等恢复，不触发外部动作。工具只验证记录转换，不实际执行/查询外部服务，也不自动获得授权。

`cases/external-interruption/inputs/external/service.py` 是隔离模拟服务；其调用/应用/查询计数可验证中断恢复协议。模拟通过不证明真实支付、部署或供应商接口可用；真实恢复应使用对应专业 skill 与授权查询入口。
