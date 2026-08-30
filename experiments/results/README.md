# 实验结果目录

本目录只版本化脱敏、体积可控且能够解释来源的原始结果。模型权重、数据集、缓存和临时输出
不得提交。

当前四份 `edge-llm-q4-*.json` 是 2026-07-11 至 2026-07-12 在同一台开发机采集的
**单次初步观测**，用于支撑 `docs/MODEL_BENCHMARK.md` 中的数值。采集时没有保存准确的代码
commit、Python/CUDA/llama.cpp 版本和多次重复，因此它们不能证明当前 HEAD 可严格复现，也
不能证明任何比赛硬指标已经达标。

文件说明：

- `edge-llm-q4-cpu.json`：CPU 路径；
- `edge-llm-q4-gpu.json`：GPU 冷启动；
- `edge-llm-q4-gpu-warm.json`：GPU 预热后；
- `edge-llm-q4-gpu-structured-prompt.json`：结构化短输出提示。

正式实验至少应同时保存：commit SHA、硬件与运行时清单、数据/模型校验值、完整命令、配置、
预热策略、随机种子、逐次原始结果和不少于 3 次重复的聚合统计。
