# 报价交付进度

A报价已经交付，B缺单价，待补资料后继续同一任务。

<!-- goal-delivery-progress:authoritative-json:v1 -->
```json
{
  "schema_version":1,"revision":1,"task_id":"quote-resume","title":"报价比较",
  "goal":"交付A与B报价及预算内推荐","constraints":["预算150000元","保留A已交付报价","沿用此记录"],
  "baseline_inputs":[{"path":"outputs/a-quote.json","summary":"已完成A报价120000元"}],
  "acceptance_criteria":[{"id":"A","description":"A报价准确"},{"id":"B","description":"B报价和比较推荐准确"}],
  "overall_status":"blocked",
  "units":[
    {"id":"a","title":"A报价","required":true,"status":"completed","depends_on":[],"artifacts":[{"path":"outputs/a-quote.json","sha256":"6f2f0cbaa97bbd6d66b6dd2aaae2f394466c5d2fb80a2ad5e2f9e62c4e4cebfb","summary":"已核对A报价120000元"}],"acceptance_results":[{"criterion_id":"A","status":"passed","summary":"A报价120000元已核对","evidence":["outputs/a-quote.json"]}]},
    {"id":"b","title":"B报价与推荐","required":true,"status":"blocked","depends_on":["a"],"artifacts":[],"acceptance_results":[{"criterion_id":"B","status":"blocked","summary":"缺B单价","evidence":[]}]}
  ],
  "blockers":[{"id":"b-price","summary":"等待B单价","unit_ids":["b"],"status":"open"}],
  "next_steps":["读取补充B资料","复核A并补齐B和推荐","更新同一记录"],
  "decisions":[],"operations":[],"updated_at":"2026-10-15T09:00:00+08:00"
}
```
