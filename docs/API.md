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

### `POST /v1/escalate`

由 Edge 请求协同处理。MVP 选择 `CLOUD` 或 `EDGE_FALLBACK`。

### `GET /health`

返回 Controller 与云端超时配置。

## Cloud

### `POST /v1/infer`

执行较强融合推理。

### `GET /health`

返回云端模型与模拟延迟。

## Recorder

- `POST /v1/events`：写入事件；
- `GET /v1/events?limit=100`：读取最近事件；
- `GET /v1/summary`：路由和组件聚合统计；
- `POST /v1/reset`：清空 MVP 指标数据库；
- `GET /health`：健康检查。

## 路由枚举

- `EDGE`；
- `EDGE_SAFETY`；
- `CLOUD`；
- `PEER_EDGE`（预留）；
- `EDGE_FALLBACK`。
