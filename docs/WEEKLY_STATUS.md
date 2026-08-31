# Weekly Status

## 2026-08-24 至 2026-08-30

### 本周完成

- [x] 将 DREAM-Exit、DREAM-Route 和 DREAM-Fuse 算法骨架接入仓库；
- [x] 增加双 Edge 心跳、Peer 一跳路由、deadline 实时预算和确定性保守降级；
- [x] 将 Recorder 指标写入改为业务响应外的 best-effort 异步投递；
- [x] 增加五路径冒烟设计和客户端并发压力测量 runner；
- [x] 整合工业视觉主线、个人算法报告、压力测试口径与提交状态文档；
- [x] 提交脱敏模型单次原始观测，并明确复现与指标声明边界。

### 当前验证

- 自动化测试与 Ruff 以 [提交状态与证据清单](SUBMISSION_STATUS_2026-08-30.md) 的最新记录为准；
- Docker Compose 基础/测试配置可解析；
- 当前开发机 Docker daemon 未运行，因此尚未取得当前 commit 的容器冒烟输出、Recorder
  summary 和 Dashboard 截图。

### 仍未完成

- [ ] 工业视觉和 CityFlow 真实模型/数据接入；
- [ ] 正常、弱网、断网的可重复实验矩阵；
- [ ] 带 ground truth 的冲突集与正确解决率实验；
- [ ] 双场景 0.2s、业务保持率、冲突率、能力保持率、TTFT 与内存硬指标证据；
- [ ] Adapter 约 17s 延迟的根因定位；
- [ ] 当前 commit 的真实 Docker Compose 冒烟证据。

### 下一步

先启动 Docker Desktop，按 `docs/TEST_PLAN.md` 运行五路径冒烟与并发测量并保存原始结果；
随后冻结工业视觉数据集和目标硬件，再运行双场景基线、弱网矩阵、冲突回放和消融实验。

## 2026-07-06 至 2026-07-12

### 本周目标

1. 明确整个云边协同系统如何搭建；
2. 明确边缘端与云端如何在单机模拟；
3. 明确 Demo 需要的技术模块；
4. 输出系统架构设计和初步实现方案。

### 已完成

- [x] 确定“边缘自治 + 中央协同调度 + 云端增强”；
- [x] 确定 FastAPI + Docker Compose + Toxiproxy + SQLite 技术栈；
- [x] 明确四条 MVP 路径；
- [x] 完成系统架构、流程和部署图；
- [x] 建立规范化 Roadmap、API、测试计划和 ADR；
- [x] 生成可运行的 MVP v0.1 代码；
- [x] 添加单元测试、CI 和 Dashboard；
- [x] 在无 Docker 环境下完成真实多进程集成测试，EDGE/CLOUD/EDGE_FALLBACK/EDGE_SAFETY 四条路径全部通过。

### 待团队本机验证

- [ ] Docker Compose 镜像构建；
- [ ] Toxiproxy 初始化；
- [ ] 四条 smoke case；
- [ ] Dashboard 显示；
- [ ] macOS、Windows/Linux 跨平台问题。

### 阻塞

- 当前执行环境无 Docker，因此尚未验证 Docker Compose 与 Toxiproxy 容器启动；FastAPI 多进程集成测试已通过；
- GitHub 私有仓库已创建，MVP、系统设计、Roadmap、ADR 与 CI 已写入 `main`。

### 下周建议

- 先完成团队本地 MVP 验收；
- 再选择工业场景数据集和真实轻量模型；
- 不提前开展多节点和正式前端开发。
