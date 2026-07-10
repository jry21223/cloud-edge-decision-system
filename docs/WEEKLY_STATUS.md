# Weekly Status

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
- GitHub 连接未提供“创建新仓库”操作，需要在 GitHub 创建空仓库后推送，或由已有仓库承载。

### 下周建议

- 先完成团队本地 MVP 验收；
- 再选择工业场景数据集和真实轻量模型；
- 不提前开展多节点和正式前端开发。
