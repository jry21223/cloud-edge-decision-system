# 初步实现方案

## 1. 技术栈

| 层次 | MVP | 后续正式版本 |
|---|---|---|
| 后端 | Python 3.12、FastAPI、Pydantic | 保持不变 |
| 服务通信 | HTTP/JSON、httpx | 必要时再评估 gRPC |
| 编排 | Docker Compose | 不计划上 Kubernetes |
| 边缘推理 | 可解释规则模型 | XGBoost、ONNX Runtime、可选 llama.cpp |
| 云端推理 | 模拟融合模型 | OpenAI-compatible API、本地 vLLM |
| 网络模拟 | Toxiproxy | Linux 补充 tc/netem |
| 数据 | SQLite、SQLAlchemy | 规模需要时再迁移 PostgreSQL |
| 展示 | FastAPI + 原生 HTML/JS | React、TypeScript、ECharts、React Flow |
| 测试 | pytest、冒烟脚本 | Locust、实验自动化 |
| CI | GitHub Actions | 保持不变 |

## 2. 服务与端口

```text
Client
  ↓
Edge A :8001
  ├─ EDGE / EDGE_SAFETY → 返回
  └─ 低 confidence → Controller :8002
                          ↓
                    Toxiproxy :8666
                          ↓
                    Cloud Node :8003

所有组件 → Recorder :8004 → Dashboard :8080
```

## 3. MVP 数据流

1. Client 向 Edge `POST /v1/tasks`；
2. Edge 执行本地规则模型，获得 prediction 和 confidence；
3. 高风险任务直接 `EDGE_SAFETY`；
4. 高 confidence 任务直接 `EDGE`；
5. 其余任务调用 Controller；
6. Controller 根据 deadline 和云端可用性调用 Cloud；
7. 云端成功返回则 `CLOUD`；
8. 云端超时或断开则 `EDGE_FALLBACK`；
9. 决策写入 Recorder；
10. Dashboard 每 2 秒拉取统计和最近事件。

## 4. 模型替换接口

MVP 规则模型只是占位。真实模型应保持以下输出：

```json
{
  "prediction": "warning",
  "confidence": 0.76,
  "action": "inspect",
  "reason": "模型或规则说明",
  "latency_ms": 12.4,
  "model_name": "industrial-onnx-v1",
  "node_id": "edge-a"
}
```

调度器只依赖这一结构，不依赖模型内部实现。

## 5. 第一周实现顺序

1. 团队成员 clone 仓库并运行单元测试；
2. Docker Compose 启动全部服务；
3. 跑通四个 smoke case；
4. 验证 Recorder 和 Dashboard；
5. 手动禁用 Toxiproxy 云端代理，验证降级；
6. 收集本地运行问题并修复；
7. 冻结 MVP API，进入真实模型和网络实验。

## 6. Definition of Done

模块完成必须同时满足：

- 代码提交；
- API 文档更新；
- 单元测试通过；
- 端到端路径通过；
- 异常路径有测试；
- 日志字段完整；
- 运行命令写入 README；
- 重要技术决定有 ADR。
