# API 设计

所有接口使用 JSON。FastAPI 启动后可访问 `/docs` 查看 OpenAPI 页面。

## Edge

### `POST /v1/tasks`

接收业务任务并返回最终决策。

请求：

```json
{
  "task_id": "case-001",
  "scene": "industrial",
  "payload": {
    "temperature": 84,
    "vibration": 7.2,
    "current": 16.2,
    "log": "间歇异响"
  },
  "risk_level": "high",
  "deadline_ms": 900,
  "metadata": {"force_confidence": 0.55}
}
```

响应：

```json
{
  "task_id": "case-001",
  "route": "CLOUD",
  "final_prediction": "critical",
  "final_action": "shutdown",
  "final_confidence": 0.96,
  "decision_reason": "edge confidence 不足，云端在时间预算内返回",
  "edge_result": {},
  "cloud_result": {},
  "degraded": false,
  "total_latency_ms": 380.4
}
```

> `metadata.force_confidence` 默认会被忽略。仅在使用 `compose.test.yml` 或显式设置
> `ALLOW_TEST_CONTROLS=true` 的隔离测试环境中有效；控制被应用时会写入 Edge 日志和
> Recorder 决策事件的 `edge_result.reason`。共享或生产环境不得开启该配置。

### `POST /v1/infer`

只执行边缘模型，不进行调度。

### `GET /health`

返回节点健康和本地阈值。

## Controller

### `POST /v1/arbitrate`

接收同一任务的 2--8 个边缘提案。DREAM-Fuse 将校准置信度、节点历史可靠度、观测
新鲜度、时空一致性和策略版本组合为证据权重，再对相同 `prediction + action` 聚合。
达到安全触发条件的 `critical`/`incident` 证据立即返回本地保守动作；普通冲突的共识不足时
返回 `CLOUD` 并标记 `requires_cloud_review=true`。响应中的 `resolution_success` 只表示当前
原型已自主形成稳定结果，不等于结果与真实标签一致。

### `POST /v1/escalate`

由 Edge 请求协同处理。Controller 使用 DREAM-Route 对满足剩余 deadline 的健康 Peer、Cloud
和本地保守降级统一评分，并按代价顺序有限尝试；每次远端尝试前重新计算剩余 deadline。
Peer 只允许一跳且 `visited_nodes` 防环；所有可行远端路径不可用、超时或预算不足时返回
`EDGE_FALLBACK`。

### `POST /v1/nodes/heartbeat`

边缘节点上报 `node_id`、支持场景、负载、队列深度、预估时延和模型版本。Controller 以 `NODE_TTL_SECONDS`（默认 15 秒）判定节点是否健康。

### `GET /v1/nodes`

列出已注册节点及其 `last_seen`、健康状态和资源摘要；Controller 的 Peer 选择综合时延、队列、
负载和节点可靠度惩罚，并排除已访问节点。

### `GET /health`

返回 Controller 与云端超时配置。

## Cloud

### `POST /v1/infer`

当前执行可替换的模拟融合规则；真实强模型尚未接入。

### `GET /health`

返回云端模型与模拟延迟。

## Recorder

- `POST /v1/events`：写入事件；
- `GET /v1/events?limit=100`：读取最近事件；
- `GET /v1/summary`：路由、组件和仲裁指标聚合统计。其中：
  - `conflict_rate` 是仲裁前原始冲突任务占全部仲裁任务的比例；
  - `autonomous_resolution_rate` 是冲突中无需云复核即形成结果的比例；
  - `resolution_success_rate` 只在请求携带 `metadata.ground_truth_prediction` 时计算，表示带真值冲突中的正确解决率；无带真值样本时为 `null`；
- `POST /v1/reset`：清空 MVP 指标数据库；
- `GET /health`：健康检查。

## 路由枚举

- `EDGE`；
- `EDGE_SAFETY`；
- `CLOUD`；
- `PEER_EDGE`；
- `EDGE_FALLBACK`。
