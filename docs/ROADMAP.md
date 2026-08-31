# Roadmap

> 项目：Cloud-Edge Decision System  
> 当前版本：MVP v0.1  
> 最后更新：2026-08-30
> 最终提交目标：2026-08-31

## 维护规则

- 每周更新本文件的状态、风险和下一步；
- 每个阶段必须有可运行代码、验收案例和可追溯日志；
- 架构或接口发生重要变化时新增 ADR；
- 报告中的指标只能来自 `experiments/results/` 中可复现实验。

## 阶段总览

| 阶段 | 目标 | 状态 | 验收结果 |
|---|---|---|---|
| P0 架构与范围 | 架构、技术栈、MVP、测试方案 | **完成** | 文档、ADR、仓库结构已建立 |
| P1 单边缘闭环 | EDGE / CLOUD / FALLBACK / SAFETY | **进行中** | 单元逻辑完成；Docker daemon 未运行，Compose 冒烟待验证 |
| P2 弱网感知 | 延迟、丢包、断网、动态阈值 | **进行中** | deadline、动态阈值和有限回退已实现；弱网矩阵待运行 |
| P3 真实模型 | ONNX/轻量模型 + 云端 Adapter | **进行中** | 已有 GGUF Adapter 和单次测量；待复测、优化与能力评测 |
| P4 多边缘节点 | 注册、心跳、Peer 调度、冲突仲裁 | **后期扩展** | 代码保留在 `compose.peer.yml`，不进入 MVP 主线验收 |
| P5 第二场景 | 交通场景复用同一调度器 | **部分完成** | 合成输入与统一接口可运行；真实工业视觉/交通数据未接入 |
| P6 完整实验 | 五种基线与多种网络条件 | **进行中** | 并发测量 runner 已实现；基线、弱网和消融结果待采集 |
| P7 Demo 与材料 | Dashboard、报告、视频、PPT | **进行中** | 仓库文档已整合；真实演示证据、视频和 PPT 待补 |

---

## P0：架构与范围

### 输出

- [x] 项目背景与问题简述；
- [x] 系统架构设计；
- [x] 技术栈；
- [x] API 草案；
- [x] Docker Compose 模拟方案；
- [x] Roadmap、ADR、测试计划；
- [x] 第一阶段 MVP 代码骨架。

### Definition of Done

团队能够明确回答：系统由什么组成、任务如何流转、何时本地/云端/降级、如何在单机模拟、下一阶段如何分工。

---

## P1：单边缘节点闭环

### 范围

- 一个 Edge；
- 一个 Controller；
- 一个 Cloud；
- 一个 Recorder；
- Toxiproxy；
- 轻量 Dashboard。

### 验收案例

- [x] `confidence=0.95`、低风险 → `EDGE`（单元测试）；
- [ ] `confidence=0.55`、云端可用 → `CLOUD`（冒烟脚本已覆盖，Compose 待实测）；
- [x] `confidence=0.55`、远端不可行 → `EDGE_FALLBACK`（单元测试）；
- [x] `risk=critical` → `EDGE_SAFETY`（单元测试）；
- [ ] 100 条请求连续运行无崩溃；
- [ ] Recorder 中每个决策包含路径、原因和耗时。

### 当前任务

- 在团队成员电脑安装 Docker Desktop；
- 执行 `docker compose up --build -d`；
- 执行 `python scripts/smoke_test.py`；
- 修复跨平台和启动顺序问题；
- 将真实运行截图、日志和结果提交到 `docs/evidence/`。

---

## P2：弱网与动态调度

### 功能

- 通过 Toxiproxy 模拟 20/100/300ms 延迟；
- 模拟 5%/10% 丢包和云端完全断开；
- Controller 采集云端健康状态和请求耗时；
- 增加动态本地阈值；
- 增加任务 deadline 与超时预算；
- 对失败调用实施有限重试，禁止无限重试。

### 验收

- [ ] 弱网配置脚本可一键执行和恢复；
- [ ] 云端断开后任务在 deadline 内返回；
- [ ] 弱网业务完成率可计算；
- [ ] Cloud Only 与 Network-aware 对照实验完成。

---

## P3：真实模型

当前已完成：可选 GGUF 边缘模型 Adapter、规则优先的安全护栏、短标签抽取、模型预热开关，以及 DeepSeek-R1-Distill-Qwen-1.5B Q4_0 的 CPU/GPU 基准脚本与单次原始结果。当前基准只是一台开发机上的初步观测；Adapter 集成测量仍约 17 秒，尚不能据此声明赛题的能力保持率、单次推理内存或端到端时延达标。

### 边缘端

优先顺序：XGBoost/Random Forest → ONNX Runtime → 可选量化小语言模型。

### 云端

统一 OpenAI-compatible Adapter，可切换 API、本地 vLLM 或模拟云端。

### 验收

- [ ] Model Adapter 接口固定；
- [ ] Ground Truth 不进入模型请求；
- [ ] confidence 完成校准或可靠性评估；
- [ ] 记录 Accuracy/F1、平均/P95 延迟、峰值内存；
- [ ] 模型替换不破坏原有调度和降级路径。

---

## P4：多边缘节点与一致性

本阶段已移出 MVP 主线。现有 DREAM-Fuse、注册和 Peer 代码作为后期扩展保留，只能通过 `compose.peer.yml` 显式启用；默认部署和正式工业视觉验收不得依赖 Peer。

### 功能

- Edge A/B/C 注册与心跳；
- Controller Node Registry；
- Peer 调度；
- `hop_count`、`visited_nodes` 防环；`task_id` 持久化幂等待实现；
- 重叠感知事件与冲突仲裁。

### 验收

- [ ] 不存在循环转发；
- [ ] 故意构造冲突数据可重复触发；
- [ ] 冲突率、仲裁成功率有明确定义；
- [ ] 云端不可用时采用保守策略；
- [ ] 节点异常不会导致整条主链路瘫痪。

---

## P5：第二场景

### 场景

1. 工业视觉质量检测；
2. 交通拥堵与事故研判。

### 验收

- [ ] 只替换数据生成器与 Model Adapter；
- [ ] 调度器核心逻辑不针对场景硬编码；
- [ ] 两个场景均具备 Ground Truth；
- [ ] 两个场景均可运行完整网络实验。

---

## P6：完整实验

### 基线

- Cloud Only；
- Edge Only；
- Confidence Cascade；
- Network-aware Scheduler；
- Full Adaptive Scheduler。

### 指标

- Accuracy、Recall、F1；
- 平均/P95 时延、TTFT；
- 上云比例、上传字节数；
- 峰值内存；
- 弱网任务完成率；
- 冲突率与冲突解决成功率。

### 验收

- [ ] 实验配置版本化；
- [ ] 每项结论有对照组；
- [ ] 原始日志、聚合脚本和图表可追溯；
- [ ] 不使用手工编造的指标。

已提供 `scripts/benchmark_system.py`，用于记录客户端观察的成功率、deadline 达标率、路由、
正文通信量、吞吐和时延分位数。该脚本本身不注入网络故障，运行输出也不等同于弱网实验结论。

---

## P7：Demo 与提交

- 正式 React + ECharts Dashboard；
- 可视化任务路径、网络曲线、节点负载和指标；
- 一键场景切换与故障注入；
- 项目报告、源码、实验数据、视频、PPT、答辩问答；
- 8 月下旬冻结架构，只做修复和材料完善。

## 当前风险

| 风险 | 影响 | 控制措施 |
|---|---|---|
| 过早接入大模型 | 阻塞主链路 | Mock/规则模型先闭环 |
| 只做页面不做实验 | 无法证明收益 | Recorder 从 MVP 开始保留 |
| 只用 confidence | 创新性不足 | P2 加网络，P4 加负载/Peer |
| 多节点自由转发 | 环路、死锁 | 所有 Peer 由 Controller 选择 |
| 两个场景分别开发 | 工作量翻倍 | 统一 Task 和 Model Adapter |
| 指标口径模糊 | 答辩风险 | 先写定义，再跑实验 |
| 文档目标与已实现混写 | 可信度下降 | 提交状态矩阵明确“已验证 / 仅设计 / 待实测” |
