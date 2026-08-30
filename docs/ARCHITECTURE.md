# 系统架构设计

> 版本：v1.0  
> 状态：架构定稿，MVP 原型代码已实现；当前 commit 的 Compose 实测待完成

## 1. 架构结论

采用 **边缘自治 + 中央协同调度 + 云端增强** 的分层架构。

传感器或客户端首先将任务交给边缘节点。边缘节点独立完成高置信度、低风险任务和紧急安全动作；只有不确定任务才请求 Controller。Controller 根据任务风险、剩余时间预算、请求携带的网络快照和节点心跳信息，对云端、其他边缘节点和本地降级统一评分。

```mermaid
flowchart TB
    A[场景与数据层] --> B[边缘自治层 Edge]
    B -->|高置信度/低风险| C[本地快速路径]
    B -->|低置信度/需复核| D[Controller / Scheduler]
    D -->|云端可用且满足 deadline| E[Toxiproxy / tc-netem]
    E --> F[Cloud Inference]
    D -->|健康且满足 deadline| G[Peer Edge]
    D -->|远端不可用| H[EDGE_FALLBACK]
    C --> I[最终决策]
    F --> I
    G --> I
    H --> I
    I --> J[Recorder / Dashboard]
```

## 2. 关键原则

1. Client/Sensor 的首跳是 Edge，不是 Controller；
2. Controller 是协同控制面，不是所有请求的必经数据面；
3. 云端和 Controller 故障时，Edge 必须维持基本业务；
4. Edge 不进行自由递归转发，Peer Edge 由 Controller 统一选择；
5. 模型、场景和云端供应方通过 Adapter 解耦；
6. 日志、Ground Truth 和实验指标从第一版开始设计；
7. 高风险任务的本地安全动作优先于云端复核。

## 3. 组件

### Edge Node

- Preprocessor：输入校验与特征构造；
- Model Adapter：规则、XGBoost、ONNX、量化小模型的统一接口；
- Confidence & Risk：生成预测、置信度、风险和耗时；
- Local Policy：本地直返、紧急安全动作和保守降级；
- Resource Agent：当前通过环境变量上报负载、队列、预估时延和模型版本；真实在线采样待接入；
- Local Store：后续实现断网缓存和恢复同步。

### Controller

输入：任务、边缘结果、已消耗时间、网络/云端状态。  
输出：`CLOUD`、`PEER_EDGE` 或 `EDGE_FALLBACK`，并给出 `decision_reason`。

### Cloud Node

- 复杂任务推理；
- 多源信息融合；
- 后续冲突仲裁；
- 后续下发阈值、规则和模型版本。

### Recorder & Dashboard

Recorder 作为单写者写入 SQLite，避免多个服务直接并发写同一数据库。Dashboard 只读取 Recorder API。

## 4. 决策流程

```mermaid
flowchart TD
    A[任务到达 Edge] --> B[本地推理]
    B --> C{高风险或高危预测?}
    C -->|是| D[EDGE_SAFETY]
    C -->|否| E{confidence >= threshold?}
    E -->|是| F[EDGE]
    E -->|否| G[请求 Controller]
    G --> H[DREAM-Route 构造 PEER / CLOUD / FALLBACK 候选]
    H --> I{最优可行路径?}
    I -->|健康 Peer| J[PEER_EDGE]
    I -->|云端| K[CLOUD]
    I -->|远端不可行或失败| L[EDGE_FALLBACK]
```

当前在线规则可简化为：

```python
if task.risk_level == "critical" or edge_result.prediction is critical:
    return EDGE_SAFETY
if edge_result.confidence >= local_threshold:
    return EDGE
plan = rank(peer_candidates + [cloud, fallback], risk, network, load, deadline)
for route in plan:
    refresh_remaining_deadline()
    if route is feasible and remote_call_succeeds(route):
        return route
return conservative_local_fallback()
```

当前网络 RTT、抖动、丢包率和可用率来自请求 `metadata.network`，节点负载/队列来自心跳；
它们是可控实验输入，尚不是系统自动测得的实时遥测。DREAM-Fuse 已在独立
`POST /v1/arbitrate` 接口实现，但普通 `/v1/tasks` 路径尚未自动收集多个 Peer 的关联结果并触发融合。

## 5. 本地部署

```mermaid
flowchart LR
    C[Client] --> E[Edge A :8001]
    E --> S[Controller :8002]
    S --> T[Toxiproxy :8666]
    T --> CL[Cloud :8003]
    E -.events.-> R[Recorder :8004]
    S -.events.-> R
    CL -.events.-> R
    R --> D[Dashboard :8080]
```

所有节点可以在一台电脑上通过 Docker Compose 模拟。Toxiproxy 放置在 Controller 与 Cloud 之间，只影响被测试的云端链路，不修改整台开发机的网络。

## 6. 多节点扩展约束

- Node Registry + 心跳上报；
- 所有 Peer 选择由 Controller 完成；
- `hop_count <= 1`；
- `visited_nodes` 防止重复访问；
- `task_id` 作为幂等键的设计约束；持久化去重尚待实现；
- 超过 deadline 立即降级；
- 仲裁使用 DREAM-Fuse：校准置信度 × 节点可靠度 × 新鲜度 × 时空一致性 × 版本系数；
- 高风险且证据充分时立即执行保守安全动作；普通低共识冲突请求云复核；
- `resolution_success`（自主形成结果）与带 ground truth 的正确解决率分开统计。
