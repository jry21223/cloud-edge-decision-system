# ADR-0002：Peer Edge 由 Controller 统一选择

- 状态：Accepted
- 日期：2026-07-10

## 背景

边缘节点自由互相发现和递归转发容易形成环路、重复执行、超时和难以解释的状态。

## 决策

Edge 不自行递归转发。后续 Peer 调度由 Controller 的 Node Registry 统一选择，并使用 `task_id`、`hop_count`、`visited_nodes` 和 deadline 控制。

## 后果

- 简化环路和死锁控制；
- Controller 成为协同控制面的关键组件；
- Controller 不可用时回到本地降级，而不是继续水平转发。
