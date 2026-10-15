# 执行适配器

自动回归核心位于 `scripts/regress.py`；内置静态 registry 位于 `scripts/executors.py`。不加载任意 Python 插件。`--backend codex|claude` 选择协议，省略为 codex；`--executor` 仅替换对应 CLI 的可执行文件路径，不能将任意运行时变成受支持宿主。

| backend | adapter_version | 本地证据状态 |
| --- | --- | --- |
| codex | 1 | 协议与历史证据覆盖；具体版本真实状态以绑定 source/change 摘要的外部验收报告为准 |
| claude | 1 | Claude Code 2.1.63 本机 help/version 已核对；原生事件及独立模拟进程协议测试；真实模型行为 unverified |

协议测试中的执行器明确带 simulated/fake 版本标记；即使报告 passed 也不能晋升。此表不是对当前安装版本、账号或目标模型可用性的保证。
真实验收报告应在冻结源码之外保存，绑定 source_sha256/change_sha256、模型与执行身份；不得根据某次通过宣称宿主或未来版本普遍兼容。

## 适配边界

适配器负责确定性 argv、宿主指令文件/注入、模型配置、原始 stream 解析、final 提取及指标来源。核心保留执行器文件摘要、环境、进程、超时后进程树清理、冻结身份、来源快照、完整产物、客观判据、独立语义/定义/边界评审和学习门槛。新增后端须先扩展内置 registry 和协议测试，再按用户批准的模型真实验收。

新报告 schema_version=2，identity 与冻结 execution-identity 同时绑定 backend/adapter_version。缺失这两个字段的 schema_version=1 仅按历史 Codex 解释；字段缺一、未知后端或版本均拒绝。compare 拒绝跨后端/适配器版本；resume 还要求原输出目录与完整身份一致。公共 `regress.parse_trace` 保留历史 Codex 入口，`load_report` 默认完整核验证据，不能关闭 verify 后用于晋升。

## Codex

使用 `exec --json --ephemeral --skip-git-repo-check`，按角色选择 read-only/workspace-write sandbox，AGENTS.md 注入，`--output-last-message` 保存 final。保留显式模型与已有 CODEX_HOME/config.toml 配置解析，仅该适配器读取 Codex 配置。要求完整 thread/turn、匹配 final 和全部原始证据。

## Claude Code

使用 `--print --output-format stream-json --verbose --no-session-persistence`。CLAUDE.md 与 `--append-system-prompt` 注入相同评测边界。`--setting-sources ""` 不加载 user/project/local settings，显式 settings 禁 hooks；空 MCP 配置配合 `--strict-mcp-config`，禁 slash commands/skills 与 Chrome，不传插件目录、不启用权限绕过。

仅该子进程使用独立 `CLAUDE_CONFIG_DIR=<invocation>/host-state`，不复用用户的全局配置、缓存与会话目录；该目录的实际文件纳入报告证据。设置 DISABLE_AUTOUPDATER、CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC、DISABLE_TELEMETRY、DISABLE_ERROR_REPORTING 为1，禁后台更新和非必要遥测。环境值绑定 execution 原件并在 load_report 重算；不把凭据写入报告。运行者须预先通过受支持的进程环境提供认证，例如 API key 或 OAuth token；不复制全局凭据、不安装、不登录、不修改全局配置。依赖全局持久登录的环境可能不可直接运行，需按用户授权配置认证后再验收。

只读角色仅提供并允许 `Read,Glob,Grep`；写角色提供 `Read,Glob,Grep,Edit,Write,Bash`，两者均用 dontAsk 权限模式。只读不提供 Bash、MCP、网络、子代理或写工具；轨迹中出现未允许工具会拒绝 completed。写角色的工具白名单不是操作系统沙箱，仍须在隔离、获准的评测工作区运行；外部动作授权不能来自本适配器。

Claude live 必须显式提供具体且非空白的 `--model`；缺省、空字符串或纯空白在任何 CLI 探测、输出创建和执行之前拒绝，直接命令/调用入口也执行同一校验。offline 不启动模型，可保留未知模型的 unverified 报告。不读其他厂商配置，不隐式使用别名/默认/降级模型。原始 system init 的 model、assistant message.model 及 result.modelUsage（若提供）必须一致并与显式值相等。会话 ID 全程一致，tool_use/tool_result 一一闭合，result success、is_error=false、最后 assistant 文本、提取 final 相符才可 completed。错误/不完整/未知事件或缺身份为 unverified。未覆盖的新协议需先明确扩展，不猜测接受。

```text
python -B scripts/regress.py run --mode live --backend claude --executor claude --model <user-approved-exact-native-model> --skill . --output <workspace>/work/claude-live --timeout 180 --budget-seconds 600
```

该命令会调用模型，须实际获得用户对模型和运行的授权。不得把用户指定的其他模型偏好静默换成 Claude 模型。

## 指标与证据

| metric | Codex 原始来源 | Claude 原始来源 |
| --- | --- | --- |
| tokens | turn.completed.usage | claude.result.usage |
| tool_calls | item.completed | claude.assistant.tool_use |
| elapsed_seconds | 绑定的 monotonic-clock timing | 相同核心计时 |

Claude input_tokens 不含 cache read/create；归一时将它们加入 input_tokens，另保留 cached_input_tokens/cache_write_input_tokens 供解释。总成本用 input_tokens+output_tokens，不再次加缓存。usage 缺失或非负整数字段不合法时为 null/unavailable，不能补零。来源标签与值经适配器从原始事件重算；篡改 summary、argv、raw trace 或计时不能通过默认核验。学习记录层的 measured/reported/unavailable 不等于自动晋升的已核验来源。

报告验证证明本地一致性和可定位性，不能认证攻击者重写全部原始文件后的真实性；不使用合成证据宣称真实 AI 行为兼容。
