# 本地测试方案

## 1. 自动化测试

```bash
python -m pip install -r requirements-dev.txt
PYTHONPATH=src pytest -q
ruff check src tests scripts
```

当前测试覆盖：

- 高 confidence 本地路由；
- 低 confidence 请求升级；
- critical 风险立即安全动作；
- 测试控制在默认关闭时被忽略、显式开启时可追溯；
- 工业和交通场景的保守降级；
- 云端融合规则。
- 多边缘安全优先仲裁、冲突判定和确定性并列处理；
- 节点 TTL 健康判定与 Peer 候选选择；
- 健康 Peer 的一跳转发与二跳拒绝。
- 实际图像解码、质量、bbox 和 ROI 契约；
- Controller 视觉字节拒绝、节点端点白名单和 RAW/ROI 本地引用清理；
- Edge/Cloud 有界并发与运行时遥测；
- SQLite 决策幂等、迟到复核 outbox 和 DREAM-Fuse 终态；
- 完全相同仲裁重放、变更提案迟到计数、待 Cloud 复核不落自主终态；
- 事后 ground truth 附加、冲突正确率统计与标签隔离；
- 弱网 profile 契约、Cloud 实际尝试门禁和连续恢复窗口。

## 2. Docker 集成测试

```bash
cp .env.example .env
docker compose -f docker-compose.yml -f compose.test.yml up --build -d
docker compose ps
python scripts/smoke_test.py
```

`compose.test.yml` 仅用于冒烟测试，并显式将 `ALLOW_TEST_CONTROLS` 设为 `true`。
普通部署只运行 `docker compose up --build -d`，会保持该配置为 `false`。

预期：

```text
[PASS] 高置信度本地处理: EDGE
[PASS] 低置信度远端协同: PEER_EDGE
[PASS] 高风险边缘安全动作: EDGE_SAFETY
[PASS] 独立云端增强路径: CLOUD
[PASS] 云端断开本地降级: EDGE_FALLBACK
```

冒烟脚本会等待两个 Edge 的心跳注册；若 Peer 尚未就绪，低置信度用例会降级验证
`CLOUD`，同时脚本还会直接向 Controller 提交排除 Peer 的升级请求，独立覆盖云端路径。
断网用例直接向 Controller 提交排除两个 Peer、deadline 足以完成正常 Cloud 调用的升级请求；
脚本先读取 Toxiproxy 状态确认 Cloud 链路已关闭，再断言 `EDGE_FALLBACK`，从而隔离验证断网
因果。Peer 未就绪、Toxiproxy 不可访问或链路不能恢复都会使脚本以非零状态退出，不允许跳过。

## 3. 容器不可用时的多终端启动

先安装依赖并设置：

```bash
export PYTHONPATH=src
```

分别在五个终端启动：

```bash
DATABASE_URL=sqlite:////tmp/metrics.db uvicorn services.recorder.main:app --port 8004
RECORDER_URL=http://localhost:8004 CLOUD_INFERENCE_DELAY_MS=350 uvicorn services.cloud_node.main:app --port 8003
CLOUD_URL=http://localhost:8003 RECORDER_URL=http://localhost:8004 uvicorn services.controller.main:app --port 8002
CONTROLLER_URL=http://localhost:8002 RECORDER_URL=http://localhost:8004 ALLOW_TEST_CONTROLS=true uvicorn services.edge_node.main:app --port 8001
RECORDER_URL=http://localhost:8004 uvicorn services.dashboard.main:app --port 8080
```

这种模式无法使用 Toxiproxy 的断网案例，但可验证 EDGE、CLOUD 和 EDGE_SAFETY。

## 4. 弱网测试矩阵

`scripts/run_weak_network_matrix.py` 通过 Toxiproxy 2.12.0 应用并逐项核验 enabled、toxic 名称、
类型、方向、toxicity 和属性，随后对同一带标签 workload 采集业务保持率、严重漏检、误隔离、
动作匹配和恢复窗口：

| Profile | 注入条件 | 准确口径 | 预期 |
|---|---|---|---|
| `N0` | 无额外 toxic | 正常网络基线，仍记录客户端实测时延 | 低 confidence 可访问远端 |
| `RTT100` | 上、下行各增加 50ms | 约增加 100ms 往返时延，以客户端实测为准 | 总时延升高 |
| `RTT300` | 上、下行各增加 150ms | 约增加 300ms 往返时延，以客户端实测为准 | 减少不必要的远端等待 |
| `BW5M` | 上、下行各限制 625KB/s | 名义 5Mbps TCP 正文带宽，不等于完整链路吞吐 | 通信成本和时延上升 |
| `CF5` | `reset_peer`，`toxicity=0.05` | **5% 概率的 TCP 连接重置，不是 5% packet loss** | 失败请求进入分母并触发安全回退 |
| `OFFLINE` | Cloud 代理关闭 | 确定性 TCP 断网 | deadline 内 `EDGE_FALLBACK` |

```bash
python scripts/run_weak_network_matrix.py \
  --profiles N0 RTT100 RTT300 BW5M CF5 OFFLINE \
  --requests 100 \
  --concurrency 10 \
  --scene industrial \
  --output-dir experiments/results/weak-network-industrial
```

该 runner 的默认 workload 是隔离的 `Client -> Controller /v1/escalate -> Toxiproxy -> Cloud`：
请求显式排除 Peer，并要求每个结果的 `attempted_routes` 都含 `CLOUD`，否则整轮失败。这样能证明
故障确实作用于被测 Cloud 代理，但它跳过真实 Edge 图像入口、使用合成遥测字段，不能作为完整
Edge-to-Cloud 图像端到端时延或模型能力证据。N0 必须是首个 profile 且保持率必须大于 0。

每个故障 profile 恢复 N0 后，以连续的 5 秒业务窗口聚合若干请求批次；窗口保持率达到
`0.95 * N0` 且连续满足 3 个窗口，才记录恢复。这里是相邻固定窗口的工程门禁，不是逐请求滑动
窗口，也不替代生产长稳实验。

CF5 的概率作用于 Toxiproxy 建立的 TCP connection；HTTP keep-alive、连接数和随机性都会使
单轮观测比例偏离 5%，因此报告必须写“随机连接故障”并同时给出实际失败率，禁止写成丢包率。
Toxiproxy 2.12.0 不提供 packet-level loss toxic。真实 1%/5%/10% packet loss 仍是外部待办，
必须使用 Linux `tc netem` 或等价网关单独注入并保存命令、时间线和实测证据；在该证据完成前，
不得宣称 packet loss 矩阵已经验证。

普通 `scripts/benchmark_system.py` 仍只观测当前环境，不会自行注入网络故障；单独运行它不能
作为弱网实验完成的证据。

## 5. 并发压力测试

在服务已经启动、网络条件已经固定后运行：

```bash
python scripts/benchmark_system.py \
  --url http://localhost:8001/v1/tasks \
  --requests 100 \
  --concurrency 10 \
  --scene industrial \
  --deadline-ms 200 \
  --output experiments/results/system-industrial-c10.json
```

交通场景只需将 `--scene` 改为 `traffic`。输出包含客户端观察到的成功率、deadline 达标率、
吞吐、路由分布、请求/响应正文流量以及平均/P50/P95/P99 时延。计时不包含信号量排队，字节数
不包含 HTTP/TLS 头，因此两项必须按报告中的 `measurement_notes` 解释，不能与服务器内部推理
耗时或完整网络流量混用。建议至少运行并发 1、5、10、20 四档，每档预热后重复 3 次。

## 6. 指标计算口径

- 端到端时延：客户端发出 POST 到完整响应体接收完毕；同时报告平均、P50、P95、P99；
- deadline 达标率：在 deadline 内成功返回的请求数 / 全部请求数；
- 弱网业务保持率：弱网期间完成预先定义“基本功能”的任务数 / 弱网任务总数；
- 严重漏检：严重任务最终动作不属于 `shutdown/isolate/quarantine/close_lane` 的数量；
- 动作匹配率：带期望动作的合成任务中最终动作完全一致的比例；它仍不等同于真实模型准确率；
- 原始冲突率：仲裁前输出矛盾的关联任务数 / 全部多节点关联任务数；
- 正确解决率：最终结果与 ground truth 一致的冲突数 / 带真值冲突总数；
- 自主形成结果率：无需云复核而形成一致结果的冲突数 / 冲突总数，不等于正确解决率；
- 通信成本：runner 当前只测 HTTP 正文；完整口径需另采集协议头、重传和模型同步流量。

## 7. 测试证据

每轮集成测试保存：

- commit SHA；
- 环境和硬件；
- `.env` 中非敏感配置；
- smoke 输出；
- `/v1/summary` 结果；
- Dashboard 截图；
- 发现的问题和修复 commit。

提交前先执行不依赖 Docker daemon 的配置校验：

```bash
docker compose -f docker-compose.yml config -q
docker compose -f docker-compose.yml -f compose.test.yml config -q
```

配置解析通过只证明 YAML 合法；只有 daemon 正常、容器健康并且冒烟脚本真实运行通过后，才能
写“Compose 冒烟通过”。当前可复核状态统一记录在
[提交状态与证据清单](SUBMISSION_STATUS_2026-08-30.md)。
