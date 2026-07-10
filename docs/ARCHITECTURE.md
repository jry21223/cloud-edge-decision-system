# 系统架构设计

> 版本：v1.0  
> 状态：架构定稿，MVP v0.1 已实现

## 1. 架构结论

采用 **边缘自治 + 中央协同调度 + 云端增强** 的分层架构。

传感器或客户端首先将任务交给边缘节点。边缘节点独立完成高置信度、低风险任务和紧急安全动作；只有不确定任务才请求 Controller。Controller 根据云端可用性、时间预算和后续扩展的网络/负载信息选择云端、其他边缘节点或本地降级。

```mermaid
flowchart TB
    A[场景与数据层] --> B[边缘自治层 Edge]
    B -->|高置信度/低风险| C[本地快速路径]
    B -->|低置信度/需复核| D[Controller / Scheduler]
    D -->|云端可用且满足 deadline| E[Toxiproxy / tc-netem]
    E --> F[Cloud Inference]
    D -->|后续扩展| G[Peer Edge]
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
- Resource Agent：后续采集 CPU、内存、队列与模型版本；
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
    G --> H{云端可在 deadline 内完成?}
    H -->|是| I[CLOUD]
    H -->|否| J[EDGE_FALLBACK]
```

MVP 的规则：

```python
if task.risk_level == "critical" or edge_result.prediction is critical:
    return EDGE_SAFETY
if edge_result.confidence >= local_threshold:
    return EDGE
if cloud_can_finish_before_deadline:
    return CLOUD
return EDGE_FALLBACK
```

第二阶段再加入网络 RTT、丢包率、节点 CPU/内存/队列和 Peer Edge。

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
- `task_id` 保证幂等；
- 超过 deadline 立即降级；
- 仲裁顺序：高风险优先 → 高置信度优先 → 云端裁决 → 保守策略。
