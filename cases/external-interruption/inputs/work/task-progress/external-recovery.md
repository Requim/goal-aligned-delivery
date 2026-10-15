# 中断的模拟发布

外部成功应用后连接断开，客户端仍为executing。必须先query后补账。

<!-- goal-delivery-progress:authoritative-json:v1 -->
```json
{
  "schema_version":1,"revision":1,"task_id":"external-recovery","title":"模拟发布恢复",
  "goal":"查询隔离模拟服务并补记已成功的外部操作","constraints":["仅授权查询与补账","不得再次apply","不得访问真实服务"],
  "baseline_inputs":[{"path":"external/service-state.json","summary":"模拟服务持久状态"}],
  "acceptance_criteria":[{"id":"recover","description":"确认服务成功且补记同一操作，不重复执行"}],
  "overall_status":"in_progress",
  "units":[{"id":"recover","title":"查询与补账","required":true,"status":"in_progress","depends_on":[],"artifacts":[],"acceptance_results":[{"criterion_id":"recover","status":"unverified","summary":"待查询","evidence":[]}]}],
  "blockers":[],"next_steps":["只查询服务状态","保存查询证据并补记同一操作"],
  "decisions":[],
  "operations":[{
    "operation_id":"publish-1","target":"isolated-simulator","target_revision":"r1",
    "authorization":"此前授权首次应用；本轮只授权查询与补账","idempotency_key":"publish-key-1",
    "status":"executing","evidence":["首次调用意图已登记；连接在服务成功后断开"],
    "query_observation":null,"authorization_still_valid":false,
    "attempt":{"attempt_id":"attempt-1","sequence":1,"started_at":"2026-10-15T09:00:00+08:00"},
    "recovery_required":true
  }],
  "updated_at":"2026-10-15T09:00:00+08:00"
}
```
