# ADR-0005：默认 MVP 收缩为 Edge–Controller–Cloud

## 状态

已接受，2026-08-31。

## 背景

最终范围要求工业零部件表面缺陷检测作为主线，MVP 使用 Edge–Controller–Cloud，Peer Edge
只保留设计并在后期扩展。PR #7 合并时，默认 Compose 已包含 Edge B，视觉链路也默认收集
Peer 证据，导致实现范围超出最终方案并使主线冒烟依赖 Peer 路由代价。

## 决定

1. 默认 `docker-compose.yml` 只启动一个 Edge、Controller 和 Cloud；Recorder、Dashboard、
   Toxiproxy 是支撑组件。
2. Controller 默认 `PEER_ENABLED=false`，默认白名单不包含 Edge B。
3. Edge B、Peer 路由与 DREAM-Fuse 代码保留，通过 `compose.peer.yml` 显式启用。
4. MVP 冒烟只验收 EDGE、EDGE_SAFETY、CLOUD 和 EDGE_FALLBACK。
5. Peer 指标不得混入默认工业视觉主线结果；Peer 研究文档必须标注为后期扩展。
6. 边缘大模型压缩仍是离线扩展，不替代 YOLO + EfficientAD 工业视觉主线。

## 后果

- 默认部署和验收口径更小、更稳定，并与最终范围一致。
- Peer 代码仍可继续研究，不需要删除或破坏现有接口。
- 旧证据中的 PEER_EDGE 结果保留为历史记录，但不代表当前 MVP。
- 若未来重新启用 Peer，必须单独运行扩展 Compose、冲突数据集和多节点指标验收。
