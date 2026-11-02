# 城市集中供热热网监控系统

多热源、多热力站的区域集中供热监控平台：实时监控、工艺图、远程调控、多维统计分析、AI 运行分析与预测。

- 后端：Python 3.11+ · FastAPI · numpy / pandas / scikit-learn（内置基于传热机理的热网仿真器，代替 SCADA 采集层）
- 前端：Vue 3 · TypeScript · Vite · Element Plus · ECharts（深色调度大屏风格）

## 快速开始

```bash
# 后端
cd backend
pip install -r requirements.txt
uvicorn app.main:app --port 8000

# 前端（另开终端）
cd frontend
npm install
npm run dev          # http://localhost:5173 ，/api 与 /ws 自动代理到 8000
```

或直接执行根目录的 `./run.sh` 同时启动两端。测试：`cd backend && pytest -q`，前端类型检查：`cd frontend && npm run typecheck`。

## 功能一览

| 模块 | 页面 | 要点 |
|---|---|---|
| 全网总览 | `/overview` | KPI（负荷、今日热/电/水/成本、室温）、热网拓扑（供回水动画、状态着色、点击跳转）、热源负荷率、实时报警、72h 负荷趋势、室温分布 |
| 热源监控 | `/sources` 多热源同页 | 负荷率仪表、关键参数、机组运行状态、7 天多热源出力堆叠对比 |
| | `/sources/:id` 工艺图 | 热电联产首站（汽水换热器×3）、燃气锅炉房（锅炉×3）、热泵能源站（热泵+电极锅炉+蓄热罐）三种工艺；所带热力站；72h 负荷预测 |
| 热力站监控 | `/stations` 多站同页 | 卡片/表格两种视图，按热源/状态筛选，报警高亮 |
| | `/stations/:id` 工艺图 + 控制 | 一次侧调节阀—板换—二次循环泵—补水—楼栋全流程实时数据；**远程控制**：气候补偿曲线（拖拽编辑 + 载入 AI 推荐）、阀门开度、控制模式/定供温、循环泵频率 |
| 统计分析 | `/analytics` | 日运行分析（日环比/同比、逐时曲线、结论、分对象报表、CSV 导出）；节能分析（度日数修正单耗、同比节能量、标煤/CO₂、过热挖潜、室温–能耗矩阵、热源单位成本）；多维统计（指标 × 对象 × 时间粒度透视、热力图） |
| AI 运行分析 | `/ai` | 综合诊断与运行评分、负荷预测（P10/P50/P90）、异常检测、一站一策曲线优化（可一键生成调控草案）、调控效果评估（DiD） |
| 调控与审计 | `/commands` | 指令记录（可回滚）、草案、完整审计日志 |
| 报警中心 | `/alarms` | 活动报警确认、报警历史 |

## 调控指令安全链路

参照《智慧供热平台 · 智能体工具清单设计》第六节，所有控制命令（无论人工还是 AI 发起）都走五步：

```
草拟(draft) → 安全校验(check) → 审批签发令牌(approve) → 下发(execute) → 效果跟踪/回滚(effect/rollback)
```

- **安全校验在服务端强制执行**：限值、单次变化率、每小时频次、通讯状态、防冻联锁、曲线单调性，以及**仿真验证**（用机理模型预测变更后的稳态室温/供温/热功率，预测室温 < 16℃ 直接拦截）。
- **令牌**：HMAC 签名、与草案绑定、10 分钟有效、一次性使用；无有效令牌 `execute` 一律拒绝。
- **自治等级**：L3 站点（示例中的锦绣花园站）在校验全部 pass 时由安全网关自动签发；其他站需人工确认。
- 每一步写审计日志；回滚恢复到下发前的完整设定。

## 算法说明（数据分析口径）

### 1. 热网仿真（`backend/app/sim/physics.py`）
每个热力站是“一次侧—逆流板换（ε-NTU）—二次侧—建筑”的稳态模型，三个方程联立有闭式解：

```
Q = E·(Ts1 − Tr2)        E = ε·Cmin（换热器）
Q = k1·(Tm − Tin)        Tm = (Ts2 + Tr2)/2（散热器）
Q = k2·(Tin − Tout)      （围护结构）
```
阀门为等百分比特性，远端站资用压差随负荷平方衰减（模拟“近热远冷”）。气候补偿模式下用二分法反求阀位。实时引擎在此基础上加入阀门行程速度和温度一阶惯性，所以调控后能看到参数逐步变化。历史数据包含上一采暖季（同比基准）和本季，并注入了三类故障：ST03 一次回水温度传感器漂移、ST07 二网泄漏、ST12 间歇通讯中断。

### 2. 能耗口径（`services/analytics.py`）
- 度日数 `HDD = Σ max(18 − Tout_h, 0) / 24`
- **度日数修正单耗** `= 热量 kJ / (面积 m² × HDD)`，单位 kJ/(m²·℃·d)。直接比较 GJ/m² 会把“天冷”误判为“耗能高”，修正后才能跨站、跨时段、同比对标。
- 同比基准取上一采暖季同日历区间；过热挖潜按 `ΔQ/Q ≈ (Tin − 20)/(Tin − Tout)` 估算。

### 3. 负荷预测（`services/ai.py · forecast`）
- 特征：当前/滞后/滑动平均室外温度、时段（sin/cos 编码）、星期。
- 均值模型：GradientBoosting（平方损失）。
- **预测区间**：时间序列交叉验证（`TimeSeriesSplit`）得到**样本外**相对残差，取 10%/90% 分位数构造 P10/P90（split conformal 思路），并按预见期 √h 适度放宽。
- 回测：最后 48h 完全留出，报告 MAPE 和区间**样本外覆盖率**（名义 80%，实测约 65–80%）。
- 开发中踩过的坑：最初直接用分位数损失的 GBM 做 P10/P90，回测覆盖率只有 50%——分位数损失的梯度只有 ±α 的符号，小学习率下模型几乎停在初始常数，P10 变成一条平线。改为“均值模型 + 样本外残差分位数”后区间才随负荷形态变化。

### 4. 异常检测（`anomalies`）
每站以前 7 天为基线，用中位数/IQR 做稳健标准化，再对全部站点的多变量特征（一/二次侧热量偏差、单位面积补水、供温跟踪偏差、室温、一次温差、阀位）训练 Isolation Forest。命中小时数 ≥ 4 才形成事件，用 |z| 最大的特征判定疑似类型并附证据。**一/二次侧热量平衡**是很好的物理一致性特征：二次侧自洽而一次侧计算热量偏小，就指向一次回水测温点漂移。与调控记录关联，曲线调整引起的变化标记为“已知原因”，避免误报。

### 5. 一站一策曲线优化（`curve_opt`）
气候补偿运行时 Ts2 本身是 Tout 的函数（相关系数≈−1），直接回归 `Tin ~ Ts2 + Tout` **不可辨识**（会得到负的敏感度）。因此采用**灰箱模型**——用传热机理约束结构、用数据辨识参数：

```
(Tin − Tout) = α·(Tm − Tout)        α = k1/(k1+k2)
ΔT2 = β·(Tin − Tout)
⇒ Ts2* = Tout + (20 − Tout)/α + β(20 − Tout)/2
```
单参数过原点最小二乘，可外推到更冷的工况（界面上标出外推区）。推荐曲线经限幅、单调化后可一键生成调控草案，走同一条安全链路。

### 6. 调控效果评估（`control_effect`）
双重差分（DiD）：`(处理站 后/前 − 1) − (对照组 后/前 − 1)`，指标为度日数修正单耗，对照组为同期未调整且无异常的站点，从而扣除天气和共同趋势的影响。

## 目录结构

```
backend/
  app/config.py            拓扑、设备、安全边界配置（真实项目来自物模型/台账）
  app/sim/                 weather 气象 · physics 机理模型 · history 历史库 · realtime 实时引擎与报警
  app/services/            control 调控链路 · analytics 统计 · ai 预测/诊断
  app/main.py              REST + WebSocket（统一返回封套 ok/summary/data/evidence/confidence/warnings/trace_id）
  tests/test_api.py        机理一致性、调控链路、统计、AI 的 13 项测试
frontend/src/
  components/diagrams/     StationDiagram / SourceDiagram / NetworkTopology 工艺图（SVG，实时绑定 + 流动动画）
  components/              ClimateCurveEditor（可拖拽曲线）· CommandWizard（五步下发）· EChart · KpiCard
  views/                   各页面；analytics/ 与 ai/ 为子页面
  stores/realtime.ts       WebSocket 实时数据（断线自动重连并回退 HTTP 轮询）
```

## 接入真实系统时需要替换的部分

| 当前（演示） | 生产替换 |
|---|---|
| `sim/realtime.py` 仿真引擎 | SCADA/PLC 采集（OPC UA、Modbus、MQTT），点位映射到标准指标名 |
| `HistoryStore` 内存 DataFrame | 时序库（TDengine / TimescaleDB / InfluxDB） |
| `ControlService._apply` 改内存状态 | 指令总线下发到 PLC，并以设备回读确认回执 |
| 令牌密钥进程内随机生成、无用户体系 | 独立安全网关 + RBAC（智能体权限 = 智能体白名单 ∩ 用户权限）、密钥管理 |
| 草案/指令/审计存内存 | 关系库持久化，审计至少保存一个完整采暖季 |

所有 AI 与调控接口已使用设计文档中的统一封套，后续可直接包装成 MCP / Function Calling 工具（如 `predict_heat_load`、`analyze_anomaly_detect`、`optimize_station_curve`、`control_draft_command` 等）。
