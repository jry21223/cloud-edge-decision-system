# P3：边缘 GGUF 模型基准记录

## 目的与边界

本记录保存端侧大模型的初步测量，不替代赛题所要求的端到端指标、能力保持率或正式数据集准确率。模型权重不提交到 Git；四份脱敏 JSON 作为本表的原始观测一并提交到 `experiments/results/`。采集时未记录精确软件 commit 和完整运行时版本，因此这些结果可核对数值，但还不是当前 HEAD 的严格复现实验证据。

## 固定条件

- 日期：2026-07-11
- 主机：Intel Core i9-14900HX，NVIDIA GeForce RTX 5070 Laptop GPU（8GB），约 16GB 系统内存
- 模型：`DeepSeek-R1-Distill-Qwen-1.5B-Q4_0-GGUF`
- GGUF 文件：1016.83MB
- 输入：工业轴承异常诊断提示，65 tokens
- 上下文：512 tokens；目标最大输出：64 tokens

## 实测结果

| 模式 | 生成 tokens | TTFT | 生成总耗时 | 吞吐 | 进程 RSS 峰值 | 显存峰值 | 结论 |
|---|---:|---:|---:|---:|---:|---:|---|
| GPU 冷启动（`--gpu-layers -1`） | 31 | 27,379.27ms | 38,415.46ms | 0.81 tok/s | 1,791.33MB | 3,913MB | 首次 CUDA 内核初始化不可直接对外服务 |
| GPU 预热后（预热 8 tokens） | 32 | 6.99ms | 174.45ms | 183.43 tok/s | 1,788.47MB | 3,575MB | 本次裸模型生成耗时处于 0.2s 量级 |
| CPU（`--gpu-layers 0`） | 26 | 15,994.29ms | 16,805.40ms | 1.55 tok/s | 1,507.05MB | 2,766MB* | 不适合该场景实时推理 |

\* CUDA 版运行库仍会初始化 GPU 上下文；该列不能解释为 CPU 推理使用了模型层 GPU 卸载。

## Adapter 集成复测

`LlamaEdgeAdapter` 已接入边缘服务，使用短标签输出并以规则完成动作映射。该实测成功返回 `warning → inspect`，但在同一硬件、已执行预热的条件下，`infer_locally` 耗时 **17,086.69ms**。这与上表的裸 `create_completion` 微基准有显著差异，尚未完成根因定位。

因此，当前只能将 Adapter 作为离线模型验证和安全融合样例；实时服务默认使用 `EDGE_INFERENCE_BACKEND=rule`。不得使用裸模型 174.45ms 的结果宣称系统端到端已达赛题时延要求。

## 当前可得结论

1. 使用该 GPU 与量化模型时，应在 FastAPI 就绪前完成预热；`EDGE_LLM_WARM_ON_START=true` 会让健康检查仅在预热完成后通过。
2. 裸模型本次测量达到该提示词的 0.2 秒级，但当前 Adapter 集成测量为 17.09 秒；在该差异定位并修复前，不能测量或宣称 FastAPI、决策路由、日志写入和网络链路后的端到端 P95 达标。
3. 进程 RSS 峰值为约 1.79GB，高于赛题所述的 1.5GB 单次推理内存门槛；显存增量约为 1.4--1.5GB。不同口径不能混用，当前不得宣称内存达标。
4. 需要以同一工业/交通验证集对比云端全量模型，报告准确率、F1、任务成功率与能力保持率，才可完成 P3 验收。

## 复现命令

脚本需要 `psutil`；GGUF 实测还需要与目标 CPU/GPU 环境匹配的 `llama-cpp-python`。两者是
可选模型实验依赖，不包含在默认规则后端的运行依赖中。正式复测必须同时保存 commit SHA、
Python/CUDA/llama.cpp 版本、完整命令、重复次数和原始 JSON。

```bash
python scripts/benchmark_edge_llm.py \
  --model models/artifacts/deepseek-r1-distill-qwen-1.5b-q4_0/deepseek-r1-distill-qwen-1.5b-q4_0.gguf \
  --context 512 \
  --max-tokens 64 \
  --warmup-tokens 8 \
  --gpu-layers -1 \
  --output experiments/results/edge-llm-q4-gpu-warm.json
```
