# 仓库结构规范

```text
cloud-edge-decision-system/
├── .github/                 # CI、Issue 与 PR 模板
├── datasets/                # 可公开或可复现实验数据
├── docs/                    # 架构、API、Roadmap、ADR 与周报
│   ├── architecture/        # Mermaid 源文件
│   ├── assets/              # SVG 架构图
│   ├── decisions/           # Architecture Decision Records
│   ├── evidence/            # 可复现实证与测试输出
│   └── roadmap/             # Roadmap 执行规范与模板
├── experiments/             # 实验配置与结果
├── scripts/                 # 环境初始化和冒烟测试
├── src/common/              # 通用 Schema 与客户端
├── src/services/            # Edge、Controller、Cloud 等服务
└── tests/                   # 单元与集成测试
```

规则：

1. 生成的 PDF、DOCX 与大模型权重不直接提交；
2. 架构图同时保留 Mermaid 源文件和 SVG；
3. 实验结论必须在 `docs/evidence/` 或 `experiments/results/` 留存证据；
4. 新增服务必须包含 `/health`、Schema、测试和文档；
5. 所有架构级变更必须新增 ADR。
