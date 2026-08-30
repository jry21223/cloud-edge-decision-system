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

P2 阶段需要形成脚本，覆盖：

| 网络配置 | 延迟 | 丢包 | 预期 |
|---|---:|---:|---|
| 正常 | 20ms | 0% | 低 confidence 可上云 |
| 一般 | 100ms | 0% | 上云但总时延升高 |
| 弱网 | 300ms | 5% | 调度器减少上云 |
| 严重弱网 | 500ms | 10% | 更多 FALLBACK |
| 断网 | - | 100% | deadline 内 FALLBACK |

网络条件由 Toxiproxy 或 `tc-netem` 独立设置并记录；并发 runner 本身不注入故障，不能只凭
runner 输出宣称“弱网实验已完成”。

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
