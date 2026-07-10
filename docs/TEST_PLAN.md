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
- 工业和交通场景的保守降级；
- 云端融合规则。

## 2. Docker 集成测试

```bash
cp .env.example .env
docker compose up --build -d
docker compose ps
python scripts/smoke_test.py
```

预期：

```text
[PASS] 高置信度本地处理: EDGE
[PASS] 低置信度正常上云: CLOUD
[PASS] 高风险边缘安全动作: EDGE_SAFETY
[PASS] 云端断开本地降级: EDGE_FALLBACK
```

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

## 5. 测试证据

每轮集成测试保存：

- commit SHA；
- 环境和硬件；
- `.env` 中非敏感配置；
- smoke 输出；
- `/v1/summary` 结果；
- Dashboard 截图；
- 发现的问题和修复 commit。
