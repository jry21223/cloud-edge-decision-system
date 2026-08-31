# API 设计

所有接口使用 JSON。FastAPI 启动后可访问 `/docs` 查看 OpenAPI 页面。

## Edge

### `POST /v1/tasks`

接收业务任务并返回最终决策。无 `image` 时保持原有遥测任务兼容；有 `image` 时执行视觉质量
门控、边缘 Adapter、纯控制面选路和 Edge 直传。视觉任务以 `task_id + 规范化请求 SHA-256`
幂等：相同请求返回已冻结结果，同 ID 不同内容返回 `409`。

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

视觉请求的 `image` 至少包含 `frame_id/width/height/mime_type/sha256/byte_size/data_base64`。
建议使用 `python scripts/submit_vision_task.py --image IMAGE_PATH` 生成并提交，不手工拼接 Base64。Controller
只会收到去掉 `data_base64` 和 `local_ref` 的描述符；ROI/RAW 字节由 Edge 直接发送给目标节点。

响应：

```json
{
  "task_id": "case-001",
  "route": "CLOUD",
  "final_prediction": "critical",
  "final_action": "shutdown",
  "final_confidence": 0.96,
  "decision_reason": "edge confidence 不足，云端在时间预算内返回",
  "attempted_routes": ["CLOUD"],
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

仲裁会校验 `proposal.node_id == proposal.result.node_id`、按节点去重、使用相对当前 UTC 的绝对
新鲜度，并拒绝总绝对证据低于门槛的陈旧批次。P0 视觉主链显式使用 `task_id` 作为
`association_id`，只聚合同一任务的多 Peer 证据；跨工位关联必须由上游提供经验证的关联 ID，
系统不会仅凭 `workpiece_id` 自动推断。

仲裁入口也会拒绝携带图像字节或 `local_ref` 的请求。`finalize=false` 的预览以及尚无终态时
`requires_cloud_review=true` 的待复核结果不会写成自主终态。首个自主
终态按 `association_id` 持久保存；完全相同的重试返回 `idempotent_replay=true` 且不增加迟到计数，
同任务但提案集合变化只记录 `late_evidence` 并返回冻结终态，不得覆盖；同关联 ID 的不同任务
载荷返回 `409`。已有终态时，后续提案即使变成低共识也只能返回原冻结结果。

### `POST /v1/routes/decide`

视觉任务的纯控制面接口。输入为不含图像字节与 Edge 本地引用的任务描述、边缘摘要和剩余
deadline；输出候选目标、`METADATA/ROI/RAW` 上传模式、timeout、候选得分及剔除原因。该接口
不转发图像，也不调用 Peer/Cloud。

当前默认 MVP 只在 Cloud 与本地回退之间选路。Peer/Fuse 代码只有叠加 `compose.peer.yml` 才启用，
不属于默认视觉任务或主线指标。

### `POST /v1/escalate`

仅用于不含图像的兼容遥测任务。带 `image` 的请求返回 `422`，必须改用上述纯控制面的
`/v1/routes/decide`，从接口层阻止 Controller 接收或代理视觉字节。Controller 使用 DREAM-Route 对满足剩余 deadline 的 Cloud
和本地保守降级统一评分，并按代价顺序有限尝试；每次远端尝试前重新计算剩余 deadline。
所有可行远端路径不可用、超时或预算不足时返回
`EDGE_FALLBACK`。响应中的 `attempted_routes` 记录实际尝试过的远端类型，供弱网实验核验请求
确实经过 Cloud 代理；它不是完整分布式 trace。

### `POST /v1/nodes/heartbeat`

边缘节点上报 `node_id`、支持场景、负载、真实等待队列、预估时延、CPU、内存、进程 RSS、
可用时的 GPU/显存、实测远端 RTT/带宽 EWMA 和模型版本。GPU 采样不可用时字段为 `null`，
不会伪造为 0。Controller 以 `NODE_TTL_SECONDS`（默认 15 秒）判定节点是否健康。节点 ID 与
`endpoint_url` 必须匹配 `TRUSTED_NODE_ENDPOINTS` 静态映射；未知节点或地址不一致返回 `403`。
设置 `NODE_REGISTRATION_TOKEN` 后还必须通过 `X-Node-Registration-Token`，否则返回 `401`。

### `GET /v1/nodes`

列出已注册节点及其 `last_seen`、健康状态和资源摘要；Controller 的 Peer 选择综合时延、队列、
负载和节点可靠度惩罚，并排除已访问节点。

### `GET /health`

返回 Controller 与云端超时配置。

## Cloud

### `POST /v1/infer`

遥测任务执行可替换的规则融合；视觉任务执行明确命名的经典图像复核基线。支持
`Idempotency-Key`，同 key/同请求只执行一次，同 key/不同请求返回 `409`；
`X-Review-Only: true` 表示 outbox 恢复后的迟到证据，不得覆盖 Edge 已冻结动作。真实强模型尚未接入。
Cloud 去重仅在当前进程的有限缓存内有效，不是持久化 exactly-once；重启或淘汰后允许重新计算。

### `GET /health`

返回云端模型、模拟延迟和 CPU/RSS/GPU、并发、队列等在线遥测。

## Recorder

- `POST /v1/events`：写入事件；
- `PUT /v1/ground-truth/{association_id}`：推理完成后附加独立真值；相同内容可幂等重放，不同
  内容返回 `409`。真值不会进入 Edge、Controller 路由或 Cloud 推理请求；
- `GET /v1/events?limit=100`：读取最近事件；
- `GET /v1/summary`：路由、组件和仲裁指标聚合统计。其中：
  - `conflict_rate` 是仲裁前原始冲突任务占全部仲裁任务的比例；
  - `autonomous_resolution_rate` 是冲突中无需云复核即形成结果的比例；
  - `resolution_success_rate` 只使用事后按 `association_id` 附加的真值，比较最终 prediction，若真值
    给出 action 则同时比较 action；无带真值冲突时为 `null`；
- `POST /v1/reset`：清空 MVP 指标数据库；
- `GET /health`：健康检查。

## 路由枚举

- `EDGE`；
- `EDGE_SAFETY`；
- `CLOUD`；
- `PEER_EDGE`；
- `EDGE_FALLBACK`。
