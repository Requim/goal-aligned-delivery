# 评价与受控学习

本文件是受保护的学习机制，自动学习不得修改。

## 先执行守卫

轻量任务直接完成、核对，不强制评价或学习。禁止落盘时不调用写命令、不准备 JSON 文件，任务/共享/自动学习全部禁写；回复给可恢复摘要并明确未持久化。`GAD_EVALUATION` 或 `GAD_LEARNING_DISABLED` 有值时不进行学习；工具拒绝 record/stage/apply/rollback，只读 summarize 虽可用也不构成评测中启用学习的理由。

具备获准文件工具与 Python、允许持久化且非评测环境的标准/严格调用，开始时读取相关 `validated_experience`，收尾记录效果和开销。能力不足时仅对话交接，明确未保存；自动晋升另需已核验适配器与独立评审。`inference` 是待证实观察，不直接成为规则。学习在前台进行，每轮600秒、一个候选，不启动自动化/后台，不自动 commit/push，不更改全局配置。

## 位置与隐私

| 内容 | 位置 |
| --- | --- |
| 私有详细评价、原始反馈、推断、轨迹引用 | `WORKSPACE/work/task-evaluations/<record-id>.json` |
| 私有候选与控制根 | `WORKSPACE/work/skill-learning/goal-aligned-delivery` |
| 候选、完整前快照、候选源码、journal | 控制根的 `candidates/<id>/` |
| 新增案例的来源评价与独立定义预审来源 | 候选私有目录的 `case-definition-source.json`，不进入共享数据 |
| 当前候选、周期、版本链 | 控制根的 `current.json`、候选状态、`versions/` 与 `head.json` |
| 私有晋升评价 | `WORKSPACE/work/task-evaluations/<candidate-id>-promotion.json` |
| 跨任务去标识指标、类别与通用经验 | `--data-dir` 下的 `experience.json` |

共享根按 `--data-dir > GAD_DATA_DIR > OS user data` 选择；显式 --data-dir 可相对当前目录，
环境路径必须非空且为绝对路径。Windows 默认 LOCALAPPDATA/goal-aligned-delivery
（缺变量则 home/AppData/Local），macOS 为 home/Library/Application Support/goal-aligned-delivery，
Linux 为 XDG_DATA_HOME/goal-aligned-delivery（缺变量则 home/.local/share）。
未显式选择且旧 home/Documents/Codex/skill-data/goal-aligned-delivery 存在时返回 pending，
要求用 --data-dir 选择原根或新根；旧字节保留，不自动迁移或创建目标。--no-write 先于路径解析。
私有目录始终在原 workspace，不随共享根或宿主改变。

所选共享根必须与 workspace 不重叠。`--data-dir` 只选共享根，不能把目标绝对路径、原 task_id、原始评价、proposal、源码、快照或 journal 放进去。候选续做/apply/rollback 必须传原 `--workspace`，不能在另一个项目里按共享索引恢复。

共享数据用随机 salt 与 task_id 的 SHA-256 token 去重；这不是加密，也不认证来源。只保存 opaque ID、类别、验收状态、去标识指标、反馈/纠错计数和显式声明 generalized/deidentified 的通用经验。自然语言脱敏仍由调用者核查，不能把原话声明成通用经验。

## record 与 summarize

从 skill 根目录调用，路径替换为实际位置：

```text
python -B scripts/learning.py summarize --category recovery --data-dir <shared-root>
python -B scripts/learning.py record --workspace <workspace> --input <evaluation.json> --data-dir <shared-root>
```

完整的最小 record 输入如下；合法省略 user_feedback/inference/metrics，不需要编造用户纠错：

```json
{
  "task_id": "private-task",
  "category": "recovery",
  "acceptance": "unverified"
}
```

category 使用通用小写类别标识。acceptance 为 `passed / failed / blocked / unverified`，省略时 unverified。可选 `user_feedback` 为 `[{text,explicit_correction}]`；`inference` 为 `[{text}]`。反馈省略按空列表使用，私有原记录保持省略；显式 null、错误类型或错误纠错布尔值都拒绝。

需要记录指标时使用实际来源，以下片段合入 record：

```json
{
  "metrics": {
    "tool_calls": {"value": null, "source": "unavailable"},
    "subagent_calls": {"value": null, "source": "unavailable"},
    "tokens": {"value": null, "source": "unavailable"},
    "elapsed": {
      "task_execution": {"value": null, "source": "unavailable"},
      "workflow_maintenance": {"value": null, "source": "unavailable"},
      "learning_validation": {"value": null, "source": "unavailable"}
    }
  }
}
```

三类时耗分别记实际任务执行、进度/评价等流程维护、学习与验证成本，单位秒，不能把一次时间重复计入多类。source 只接受 `measured / reported / unavailable`；缺项归一为 null/unavailable。仅使用实际非负有限数，未知不估成零，不接受布尔数字。记录层 measured 是来源声明，不自动成为可晋升的真实测量证据。

可选 `experience={deidentified:true,generalized:true,text:"通用经验"}`；没有验证报告时归 inference。`validation_reports` 可引用实际报告路径；工具经 `regress.load_report` 完整校验真实 live passed 与已知可比身份后，才派生 validated_experience，不能直接输入同名字段。

summarize 只读，按类别返回失败数、独立 task token 数、指标来源计数/可用数量/可用值 median，以及分开的反馈、推断和已验证经验。median 需结合来源计数解读，不能把 reported 与 measured 混为实测优势。

## stage：一个候选

来源须为同类记录中的明确用户纠错，或至少两个独立 task token 的观察；同一任务多条记录不算两任务。缺来源时保留记录，不临时伪造反馈。proposal 使用已有私有 record_id：

```json
{
  "category": "recovery",
  "kind": "functional",
  "generalized_lesson": {
    "deidentified": true,
    "generalized": true,
    "text": "恢复前核对当前记录与实际材料"
  },
  "record_ids": ["替换为本workspace实际record_id"],
  "target_cases": ["resume"],
  "changes": [
    {"path": "references/principles.md", "source": "替换为普通参考的完整新文本文件路径"}
  ]
}
```

```text
python -B scripts/learning.py stage --workspace <workspace> --skill <skill-root> --proposal <proposal.json> --data-dir <shared-root>
```

changes 每项给 path 与 content/source 二选一，提供完整新文本，UTF-8 不超过2 MiB。stage 冻结来源、案例、候选摘要与完整前快照，返回 candidate_id/private_path/required_cases/case_definition_source，不改目标。无新增案例时 case_definition_source 为 null。候选只在原 workspace 续做。

允许入口普通正文、普通 `references/*.md`、原先不存在的 `cases/<id>/*`。禁止 scripts、旧 cases（含旧空目录和原始输入/输出）、越界/链接/junction/ADS/大小写绕过及固定 authority.md/learning.md/behavior-cases.md/capabilities.md/adapters.md。能力和适配说明包含运行保护与证据边界，也不能通过自动学习改写。SKILL.md 两块保护区须各恰一对、不重复/反序/嵌套，原区段文本保持一致；不要在入口额外复制标记示例。

入口和 execution.md/progress.md/contracts.md 属执行规则，自动 required_cases 为全八类加目标案例。普通局部参考为目标案例加 lightweight/no-write/resume。所有新增案例也加入 required_cases，stage 检查其请求/输入/oracle 格式，并在私有目录冻结来源评价、明确纠错或两独立任务依据与通用经验。路径检查不能理解任意自然语言是否扩权；候选仍需独立边界评审。

## 回归与晋升门槛

先按 [行为验收](behavior-cases.md) 对冻结 baseline/candidate 运行相同案例。baseline 报告绑定原摘要，candidate 报告绑定候选摘要，候选运行传 `--baseline-skill` 取得独立 candidate_review。运行身份 skill_sha256 排除 cases；完整变更 change_manifest/change_sha256 覆盖全部案例物理字节，包括输入中的 work/outputs 等目录。case-only 候选按完整变更摘要区分 baseline/candidate。所有报告须与 stage 冻结的双方完整变更、原始案例物理 manifest、oracle、模型配置及 executor 身份一致。

新增案例须在首次 live actor 前通过独立 case_definition_review；评审绑定私有来源评价、完整请求/输入/oracle，固定核查来源忠实性、请求与输入无答案泄漏、oracle 独立性、权限/禁写边界和可判定性。失败、超时、缺来源或未验证时不启动 actor，也不能晋升。定义来源只给独立 reviewer，actor 仍只得到请求、输入与运行 skill；独立边界评审另核查包含新增案例的完整双方差异。

自动候选含新增案例时，使用 stage 返回的私有目录与来源路径。下面每个 `--case` 代表一个实际 required_cases ID，须重复传全；若候选 cases 根恰好只有全部 required_cases，可省略筛选。两种命令分别写入新的空输出目录；functional 的 candidate 命令独立运行两次，efficiency 则准备至少三对：

```text
python -B scripts/regress.py run --mode live --backend <approved-backend> --skill <installed-baseline> --cases-root <candidate-private>/skill/cases --case-definition-source <candidate-private>/case-definition-source.json --case <required-case-id> --output <baseline-output> --model <approved-model> --timeout 300 --budget-seconds 600
python -B scripts/regress.py run --mode live --backend <approved-backend> --skill <candidate-private>/skill --baseline-skill <installed-baseline> --cases-root <candidate-private>/skill/cases --case-definition-source <candidate-private>/case-definition-source.json --case <required-case-id> --output <candidate-output> --model <approved-model> --timeout 300 --budget-seconds 600
```

baseline 不传 --baseline-skill；它使用同一候选案例材料和定义来源评审。无新增案例且所选材料与 skill 内固定案例一致时，不传 --case-definition-source。

- **functional**：至少一次 baseline 目标失败复现，candidate 连续两次独立通过；全部 required_cases 无质量退步，无缺判据/未验证。
- **efficiency**：proposal 另给 `efficiency_metric=elapsed_seconds|tokens|tool_calls`；至少三对独立 baseline/candidate，数量相等、按提交顺序配对，质量无退步，required_cases 的指定实测成本合计 median 下降。

报告必须经公共 `regress.load_report` 默认完整校验；sample 之间由 `compare_reports` 核对身份与独立性。run_id 不同不够，还要实际 invocation_id、JSONL 摘要、thread_id 独立，包括定义 reviewer。resume/缓存不能冒充新样本。每份 candidate 要有精确 baseline/candidate 的 skill 与完整 change 摘要绑定、完整证据和 passed 独立边界评审。apply 复核每份新增案例报告的精确覆盖、冻结来源摘要、完整判据、completed reviewer 及可定位通过证据，并再次核对私有来源和完整快照无漂移。

offline、模拟执行器、未知身份、不完整/超时、缺真实行为/身份/完整轨迹/独立评审证据均不能晋升。缺成本指标保持 null/unavailable；只有 efficiency 要求其所选成本指标来自实际测量，functional 不因成本 unavailable 单独被拒绝。compare passed 只表示未观察到退步，不能单独授权 apply。旧报告仍可只读核对历史行为，但缺完整 change 身份或新增案例定义预审时不能充作新的自动晋升证据，不补造历史评审。

指标由已知适配器从原始证据重算：Codex tokens/tool_calls 保留 `turn.completed.usage`/`item.completed`，Claude 为 `claude.result.usage`/`claude.assistant.tool_use`；来源与身份须经 load_report 核验，未知来源不接纳。elapsed 来自绑定的 `monotonic-clock` 计时。tokens 用 total_tokens 或归一后的 input_tokens+output_tokens，不重复加缓存 Token；Claude 缓存归一见 [adapters.md](adapters.md)。缺 usage 为 unavailable，不补零。新 invocation 时间为该调用；case 时间覆盖复制、actor、快照、客观检查及 case reviewer；report 时间为 case 总和加额外定义 reviewer 与 candidate-boundary reviewer，不是完整运行墙钟，不重复加 case reviewer。旧报告未绑定计时的 elapsed 在内存归一为 null/unavailable，原文件不写回；不能拿旧摘要秒数证明提效，仍有效的原始 tokens/tools 可以使用。

## apply、周期与回退

```text
python -B scripts/learning.py apply --workspace <workspace> --candidate <id> --reports <baseline-report> <candidate-report-1> <candidate-report-2> --data-dir <shared-root>
python -B scripts/learning.py stage --workspace <workspace> --candidate <id> --new-cycle --data-dir <shared-root>
python -B scripts/learning.py apply --workspace <workspace> --candidate <id> --reports <reports...> --new-cycle --data-dir <shared-root>
python -B scripts/learning.py rollback --workspace <workspace> --candidate <id> --reason "实际回退原因" --data-dir <shared-root>
```

每轮600秒包含 stage 构造与验证；同一调用不自动续期。超时保留同一候选、checked_reports 和旧周期，回复下一步，后续调用用上述 --new-cycle 显式继续；不另起候选绕过预算，不调后台/自动化。证据不足则 pending，不 force/bypass，不自动 commit/push。

apply 在目标 OS lock 内核对完整目标、候选、前快照 CAS。journal 先持久写入逐文件 before/after、临时路径、报告引用和版本，再原子替换、逐步记账，最终核对整个目标。先写 `finalizing` 与 `finalization_complete=false`，幂等完成私有 version/head、晋升评价与共享通用经验，最后才记 `applied`、`finalization_complete=true`。

下一 apply 可继续收尾；下一次直接 stage 也会在替换 current 前检查上一候选 journal、持锁核对目标 after 摘要，补齐未完收尾，兼容旧 applied 缺完成标记。不要为了续做删除旧候选或版本文件。applied/rolled_back 是终态，可在同一 workspace 开下一候选，版本链保留 previous/head。

prepared/applying/rolling_back/conflict journal 在下一 apply/rollback 进入恢复。只把仍匹配 after 摘要的文件恢复为 before；新候选文件仅在 after 匹配时删除。用户新增文件、改动、目录替换及被改临时文件保留并标 conflict，不能强行覆盖。完整自有临时文件经摘要核对可清理；恢复可幂等重放。

rollback 必须 reason；省略 candidate 时取 current，重复默认回退为 no-op，不连退另一版。需要回退版本链上最近仍应用的上一版，明确传其 candidate ID。保留评价、报告、候选和回退历史；不恢复旧数据库覆盖用户新数据。

五个命令 stdout 单个 JSON，退出0成功，1条件不足/pending/恢复冲突，2输入或持久状态损坏。五个命令均支持 --no-write，最高优先级直接不写，不创建目录/锁/缓存，也不等价于验证了输入内容。跨平台崩溃/锁行为须在目标环境核验；协作锁与 CAS 不是文件系统事务，不能证明恶意重写全部证据时的来源真实性。
