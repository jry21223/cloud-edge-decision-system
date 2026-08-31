# 最终方案范围对齐与实施计划

> 基线：`main` 合并 PR #7 后的 MVP v0.2
> 目标：工业视觉主线 + Edge–Controller–Cloud，Peer/大模型压缩作为扩展
> 实施状态：代码范围对齐已完成；真实数据、权重和正式实验仍是外部验收门禁

## 1. 冻结范围

| 项目 | 决定 |
|---|---|
| 主场景 | 金属工业零部件表面缺陷检测 |
| MVP 工件 profile | `machined-metal-bracket`，材质 `metal`；拿到正式数据后只允许通过 profile 评审替换 |
| 已知缺陷 | `scratch`、`crack`、`pit_or_wear`、`contamination`、`missing_or_assembly` |
| Edge | YOLO 已知缺陷检测 + EfficientAD 未知异常检测 |
| Cloud | 高精度视觉/视觉大模型结构化增强复核；自由文本不直接驱动动作 |
| 第二场景 | 交通视觉只验证同一图像契约和 Adapter 迁移，不实现检测、跟踪、ReID |
| MVP 架构 | Edge–Controller–Cloud；Recorder、Dashboard、Toxiproxy 是支撑组件 |
| Peer Edge | 保留代码与独立 Compose 扩展，默认关闭，不进入 MVP 验收 |
| 大模型压缩 | 离线扩展，不进入工业视觉实时主链 |
| 现场执行 | 软件模拟 |
| PLC | 不接入 |

## 2. 已完成的代码工作

1. `IndustrialVisionProfile` 强制金属材质和五类已知缺陷；任务 workpiece 不匹配时真实模型拒绝推理。
2. `YoloEfficientAdAdapter` 通过统一 `VisionModelAdapter.infer` 融合已知缺陷和未知异常结果。
3. YOLO ONNX runtime 支持端到端 NMS 输出；EfficientAD runtime 支持分数和异常热图 ROI。
4. Cloud VLM Adapter 使用受约束 JSON，校验标签、置信度、严重度和 bbox，现场动作由确定性策略映射。
5. 交通视觉迁移探针复用图像契约和服务链，但明确输出“仅架构迁移”，不宣称视觉准确率。
6. 默认 Compose 删除 Edge B、关闭 Peer；`compose.peer.yml` 单独保留后期扩展。
7. 工业视觉评测脚本同时输出分类指标与时延、deadline、路由、通信量指标。

## 3. 外部输入门禁

以下内容没有真实输入就不能伪造为完成：

1. 有来源和许可的 `machined-metal-bracket` 图像、数据划分与逐图 Ground Truth；
2. 标签顺序与 profile 一致、带端到端 NMS 的 YOLO ONNX；
3. 输出 `anomaly_score` 与 `anomaly_map` 的 EfficientAD ONNX；
4. Cloud 视觉模型端点、模型名、密钥和数据出境许可；只有显式设置
   `CLOUD_VLM_DATA_EXPORT_APPROVED=true` 才允许发送图像；
5. 正式 Edge 硬件与冻结的 Python/ONNX Runtime/驱动环境。

缺少任一模型文件或 Cloud 配置时，对应真实后端必须拒绝启动；软件模拟结果不得用于准确率声明。

## 4. 正式验收阶段

### A. 数据与模型冻结

- 校验五类标签覆盖、类别数量、许可和 train/validation/test 泄漏；
- 记录模型 SHA-256、输入尺寸、归一化、输出名、标签顺序和阈值；
- 在同一测试集上分别保存 YOLO、EfficientAD 和组合结果。

完成标准：模型文件、profile、数据 manifest 和基线结果能由另一台机器复现。

### B. Cloud 复核

- 对 Edge 的 ROI/RAW 难例执行结构化复核；
- 验证未知标签、越界 bbox、非法 JSON、超时和 429/5xx 均进入安全回退；
- 确认 VLM 建议动作不能覆盖确定性动作策略。

完成标准：固定难例集上的响应全部通过 schema，错误注入不会放行缺陷件。

### C. 交通迁移

- 只替换为交通图像来源和迁移 Adapter；
- 验证 Edge API、Controller 选路、Cloud 超时和 Recorder 指标不需要新增场景专用分支；
- 不实现 ByteTrack、ReID、多摄像头融合。

完成标准：交通图像通过同一端到端链路，并明确标记为迁移探针而非交通模型结果。

### D. 指标与弱网

- 使用 `scripts/evaluate_industrial_vision.py` 计算每类 Precision/Recall/F1、Macro-F1、严重缺陷漏检率；
- 同时记录平均/P95 时延、deadline 达成率、Cloud 路由率和上传字节；
- 在 N0、RTT100、RTT300、BW5M、断网条件下至少重复三轮。

完成标准：原始 JSONL、环境、commit、模型哈希和聚合结果一起归档；健康接口 200 不作为验收。

## 5. 非目标

- Peer Edge、DREAM-Fuse 和多边缘冲突实验；
- 真实 PLC、相机厂商 SDK、现场执行器 ACK；
- 交通跟踪、ReID、跨摄像头身份关联；
- 在线模型训练、自动阈值下发；
- 把量化边缘 LLM 作为工业视觉主线。

## 6. 验证命令

```bash
python -m pip install -r requirements-dev.txt
PYTHONPATH=src pytest -q
ruff check src tests scripts
docker compose config --quiet
docker compose -f docker-compose.yml -f compose.test.yml config --quiet
docker compose -f docker-compose.yml -f compose.peer.yml config --quiet
docker compose -f docker-compose.yml -f compose.test.yml up --build -d
python scripts/smoke_test.py
```

Peer 扩展只在单独验证时叠加 `compose.peer.yml`，其结果不得写入 MVP 主线验收。
