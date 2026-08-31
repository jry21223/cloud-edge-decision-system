# DREAM-CE：面向截止时间、风险与证据一致性的自适应云边协同算法

## 1. 定位

DREAM-CE（Deadline, Risk and Evidence-Aware Multi-edge Cloud-Edge）面向工业视觉质量检测和
多摄像头交通感知两个差异场景。算法不替代具体感知模型，而是在统一接口上组合：

1. 边缘轻量模型快速推理；
2. 风险与弱网自适应的本地提前退出；
3. Peer Edge、Cloud与本地降级之间的约束路由；
4. 多边缘节点的可靠性加权证据融合；
5. 紧急任务的本地安全闭环。

这样可以在接入工业视觉缺陷数据、CityFlow或后续能源数据集时复用同一调度和仲裁层。

## 2. 与赛题指标的对应关系

| 赛题要求 | DREAM-CE机制 | 主要证据 |
|---|---|---|
| 平均端到端时延小于0.2s | 远端调用墙钟预算、动态本地退出、Peer/Cloud约束评分 | 各路径P50/P95、总时延均值 |
| 弱网基本业务保持率不低于90% | 网络退化降低本地退出阈值、不可用远端硬惩罚、保守降级 | 弱网期间完成任务数/总任务数 |
| 冲突率不高于5% | 时空一致性约束、时间新鲜度、模型版本和节点可靠度 | 冲突任务数/关联任务数 |
| 冲突解决成功率不低于90% | DREAM-Fuse证据聚合、低共识上云复核 | 带真值冲突中正确解决数/冲突数 |
| 边侧能力保持80%至90% | 量化模型、云端教师蒸馏、低置信度升级 | edge_score/cloud_score |
| 单次推理内存不超过1.5GB | 1B至1.5B模型4bit量化、限制上下文 | 峰值RSS和显存增量 |

## 3. DREAM-Exit：动态本地退出

边缘结果为紧急事件或任务风险为critical时，立即执行安全动作，不等待网络。普通任务的
动态置信度阈值为：

```text
T = clip(T_base + A_risk - 0.18 D_network - 0.08 P_deadline, 0.55, 0.95)
```

其中，`A_risk`随风险升高而增大；`D_network`由网络可用率、丢包、RTT和抖动构成；
`P_deadline`是已消耗deadline的比例。网络恶化时更多任务留在本地，高风险任务则需要更强
证据才能直接返回。

## 4. DREAM-Route：约束路由

对EDGE_FALLBACK、PEER_EDGE和CLOUD构造候选执行配置，估计时延、准确率、可用率、负载和
通信量。候选代价为：

```text
J(k) = L(k)/D
     + 2 W_risk (1 - Q(k))
     + 2.5 (1 - A(k))
     + 0.25 Load(k)
     + 0.05 Comm(k)
     + Penalty(k)
```

- `L(k)`：预计完成时延；
- `D`：剩余deadline；
- `Q(k)`：候选模型预期准确率或节点历史可靠度；
- `A(k)`：路径可用率；
- `Penalty(k)`：deadline不可满足、远端不可用或降级路径的惩罚。

算法优先尝试代价最小且满足deadline的路径。远端不可用或所有远端均超时时，选择保守本地
动作，保证有限时间返回。

## 5. DREAM-Fuse：多边缘证据仲裁

节点 `i` 的证据权重为：

```text
w_i = p_cal_i * reliability_i * freshness_i * topology_i * version_i
freshness_i = exp(-ln(2) * age_i / half_life)
topology_i = 0.5 + 0.5 * spatial_consistency_i
```

`p_cal_i`是经过温度缩放的置信度，`reliability_i`由节点历史正确率更新，
`spatial_consistency_i`来自交通摄像头拓扑/时间窗或工业多传感器一致性检查。相同
`prediction + action`的节点证据相加：

```text
E(y) = sum(w_i for node i supporting y)
Consensus(y) = E(y) / sum(E(all outcomes))
```

共识高于阈值时自主仲裁；证据均衡时返回CLOUD复核。高风险且获得足够支持的紧急证据立即
触发安全动作。该机制避免“某个过度自信但长期不可靠的节点”直接支配最终决策。

## 6. 场景适配

### 工业视觉质量检测

边侧使用轻量缺陷检测/异常定位模型，输出缺陷类别、位置、置信度、工件ID和处置动作。重叠
相机对同一工件/区域的时空一致性作为`spatial_consistency`，按产线、批次和缺陷类型统计的
验证正确率用于更新`node_reliability`。MIMII 工业声学保留为历史备选，不属于当前提交主线。

上述跨工位关联属于后续场景适配设计。当前 P0 视觉主链只对同一 `task_id` 的多个 Peer 结果
仲裁，不会根据工件字段自动合并不同任务，以避免未经验证的关联污染终态。

### 交通CityFlow

每个摄像头边缘节点执行检测、单摄像头跟踪和ReID特征提取；摄像头拓扑、合理通行时间和
轨迹方向生成`spatial_consistency`。云端只接收轨迹、压缩特征和疑难帧，减少视频上传量。

## 7. 指标口径

运行时的`resolution_success`表示系统是否能够在当前证据下自主形成稳定决策，不代表决策
一定正确。比赛要求的仲裁成功率必须使用带真值冲突样本计算：

```text
conflict_rate = conflicting_related_tasks / all_related_tasks
resolution_success_rate = correct_final_decisions / labeled_conflicts
```

测试或人工标注在推理完成后通过 Recorder 的
`PUT /v1/ground-truth/{association_id}` 独立附加。真值不进入 Edge/Cloud 请求，也不参与路由和
仲裁；Recorder 只有在冲突组存在事后真值时才计算 `resolution_success_rate`，避免标签泄漏，
也避免把“成功返回一个结果”误报为“仲裁正确”。

## 8. 当前实现与待接入部分

已实现：

- 动态本地退出阈值；
- 路由候选代价排序；
- 节点可靠度参与Peer选择；
- 置信度、可靠度、新鲜度、时空一致性证据融合；
- 低共识云端待复核状态与事后真值指标口径；
- 紧急安全动作和断网保守降级。

待数据到位后实现：

- 工业视觉缺陷模型、数据清单及适配器；
- CityFlow检测、跟踪、ReID与拓扑一致性适配器；
- 节点可靠度的滑动窗口在线更新；
- 边侧量化模型真实内存和TTFT测试；
- 网络压力回放及消融实验。

当前实现边界（2026-08-31 P0）：Edge/Cloud 已采集在线 CPU、RSS、可用 GPU、并发队列和服务
时间，Edge 的实际远端请求更新 RTT/带宽 EWMA；视觉 `/v1/tasks` 默认聚合可用 Peer 并调用
DREAM-Fuse，P0 关联范围为单个 `task_id`。自主终态按关联 ID 持久化，完全相同重试幂等返回，
变更提案只作为迟到证据且不得覆盖；低共识 Cloud 待复核不误写为自主终态。抖动、包级丢包、节点可靠度在线学习和
真实工业模型效果仍无正式数据证据，因此准确表述仍是“P0 软件原型闭环”，不是生产级系统或
比赛硬指标已达标。

## 9. 必须进行的消融实验

为证明改进来自算法而非模型更换，至少对比：

1. 固定置信度阈值与DREAM-Exit；
2. 固定“Peer优先”与DREAM-Route；
3. 最高置信度仲裁与DREAM-Fuse；
4. 去掉节点可靠度、去掉新鲜度、去掉时空一致性的三个变体；
5. 正常网络、弱网、断网三组条件。

如果DREAM-CE无法在这些对照中稳定降低时延/冲突或提高弱网完成率，就不能把组合机制作为
有效创新点写入最终申报材料。
