# Product Backlog

## P0 — 架构与 MVP

- [x] 边缘优先架构定稿
- [x] 单边缘节点 MVP
- [x] 五类核心调度路径的代码与冒烟案例
- [x] SQLite 指标记录
- [x] Docker Compose 方案

## P1 — 弱网实验

- [ ] Toxiproxy 延迟配置脚本
- [ ] 100ms / 300ms 延迟实验
- [ ] 丢包和断网实验
- [x] deadline 与顺序远端尝试的超时边界测试
- [ ] 弱网业务完成率统计

## P2 — 真实模型

- [ ] 工业数据集与 Ground Truth
- [ ] XGBoost 基线
- [ ] ONNX Runtime Adapter
- [ ] confidence 校准
- [ ] 峰值内存与推理时延测量

## P3 — 多边缘节点

- [x] Node Registry 与心跳
- [x] Peer Edge 中央选择（后期扩展，默认关闭）
- [x] hop_count 与 visited_nodes
- [ ] 冲突数据生成器
- [x] 冲突仲裁与指标代码骨架

## P4 — 第二场景与正式展示

- [ ] 交通场景 Adapter
- [ ] 正式 React Dashboard
- [ ] 实验对照组与图表
- [ ] 演示脚本、视频和报告
