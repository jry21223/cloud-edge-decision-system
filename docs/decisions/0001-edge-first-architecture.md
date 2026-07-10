# ADR-0001：采用 Edge-First 架构

- 状态：Accepted
- 日期：2026-07-10

## 背景

题目要求弱网或断网期间仍维持基本业务。如果所有请求先进入中央 Controller，Controller 故障会使边缘自治失去意义。

## 决策

Client/Sensor 首先请求 Edge。高 confidence、低风险任务以及紧急安全任务由 Edge 独立完成；只有需要协同时才请求 Controller。

## 后果

- 云端或 Controller 故障不阻塞本地快速路径；
- Edge 需要承担本地策略与少量状态；
- 日志必须由独立 Recorder 汇总，而不是依赖所有请求经过 Controller。
