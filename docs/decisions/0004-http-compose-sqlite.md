# ADR-0004：MVP 使用 HTTP/JSON、Docker Compose 与 SQLite

- 状态：Accepted
- 日期：2026-07-10

## 背景

当前系统节点少、主要目标是可运行和可演示。Kubernetes、gRPC、消息队列和分布式数据库会增加非核心复杂度。

## 决策

- FastAPI + HTTP/JSON；
- Docker Compose 单机编排；
- Toxiproxy 模拟云端链路；
- 独立 Recorder 作为 SQLite 单写者；
- 正式展示前端后置。

## 后果

- 容易调试和演示；
- 规模增加后可替换单个实现，但不提前优化；
- 所有接口必须保持明确的超时和幂等约束。
