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

或直接执行根目录的 `./run.sh` 同时启动两端。测试：`cd backend && pytest -q`（21 项），前端类型检查：`cd frontend && npm run typecheck`。

接入大模型智能体（MCP）见下文 [MCP 智能体工具服务](#mcp-智能体工具服务)。

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
| 调控与审计 | `/commands` | **待审批**（智能体提交的调控请求，人工批准/驳回）、指令记录（可回滚）、草案、审计日志（可筛选智能体工具调用） |
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

## MCP 智能体工具服务

`backend/mcp_server/` 按《智慧供热平台 · 智能体工具清单设计》把平台能力包装成 MCP 工具，Claude Desktop、Claude Code 或任何 MCP 客户端都可以直接调用。

### 工具清单（16 个）

| 工具 | 风险 | 功能 |
|---|---|---|
| `data_search_objects` | R0 | 按名称/简称/地址/编号查对象 ID（入口工具；支持“东湖3号站”“望湖街”这类说法） |
| `data_get_realtime` | R0 | 热源/热力站/全网实时快照，附通讯状态与当前报警 |
| `data_get_timeseries_summary` | R0 | 历史时序统计摘要（均值/极值/趋势/离群点），原始序列用 `data_ref` 引用 |
| `data_get_alarms` | R0 | 报警查询，按热源范围、级别、确认状态过滤 |
| `analyze_anomaly_detect` | R1 | 多变量异常检测，返回疑似类型、证据、处置建议 |
| `analyze_energy_benchmark` | R1 | 度日数修正单耗对标、同比节能、过热挖潜、热源单位成本 |
| `analyze_control_effect` | R1 | 双重差分评估调控效果（按指令号或站点） |
| `predict_heat_load` | R1 | 24/72/168h 负荷预测，P10/P50/P90、峰值、逐日供热量 |
| `optimize_station_curve` | R1 | 一站一策气候补偿曲线优化，返回 `plan_ref` |
| `control_get_autonomy_policy` | R0 | 自治等级、安全边界、审批人、当前设定 |
| `control_draft_command` | R1 | 草拟指令（可直接用 `plan_ref`），附稳态仿真预测 |
| `control_safety_check` | R1 | 限值/变化率/频次/联锁/通讯/仿真验证，pass·warn·block |
| `control_request_approval` | R2 | 提交值班调度审批；批准后返回一次性令牌 |
| `control_execute` | R3 | 凭令牌下发，返回指令号与设备回执 |
| `control_monitor_effect` | R0 | 下发后跟踪阀门/供温是否到位、是否建议回滚 |
| `control_rollback` | R3 | 恢复到下发前的完整设定 |

所有工具返回统一封套 `{ok, summary, data, data_ref, evidence, confidence, warnings, error?, trace_id}`（同时提供 MCP 结构化输出 schema），风险等级写在工具描述和 `meta.risk` 里，R3 工具标注 `destructiveHint`。

### 安全设计

- **智能体不能批准自己的指令。** MCP 里没有任何“批准”工具。`control_request_approval` 只登记待审批；值班调度在平台 **调控与审计 → 待审批** 中看到申请智能体、理由、安全校验结果和仿真前后对比，批准后系统才签发令牌。L3 站点在校验全部 pass 时由安全网关自动签发，与人工操作同一套规则。
- **安全校验在后端强制执行**：MCP 服务只是 REST API 的薄客户端，绕过它直接调接口也逃不过校验与令牌验证。
- **模型不搬运数据**：曲线优化结果通过 `plan_ref` 交给 `control_draft_command`，由服务端重新取方案，避免模型抄错曲线数值。
- **按智能体分权**：`--profile dispatch`（调度智能体，16 个工具）或 `--profile analyst`（只读分析，9 个，不加载任何 `control_*`）。
- **全程审计**：每次工具调用记录智能体、所代表的用户、工具、风险等级、入参（令牌不落库）、结论和 trace_id，在审计日志中可按“智能体工具调用”筛选。

### 运行与接入

先启动后端（`uvicorn app.main:app --port 8000`），再按客户端配置 MCP 服务：

```bash
# Claude Code
claude mcp add heatnet -- python /绝对路径/backend/run_mcp.py --api http://127.0.0.1:8000
```

Claude Desktop（`claude_desktop_config.json`）：

```json
{
  "mcpServers": {
    "heatnet": {
      "command": "python",
      "args": ["/绝对路径/backend/run_mcp.py", "--api", "http://127.0.0.1:8000", "--profile", "dispatch",
               "--agent", "调度智能体", "--user", "张调度"]
    }
  }
}
```

也可以以 HTTP 方式供多个客户端共用：`python run_mcp.py --transport streamable-http --port 8765`，地址 `http://127.0.0.1:8765/mcp`。参数也可用环境变量 `HEATNET_API`、`HEATNET_PROFILE`、`HEATNET_AGENT`、`HEATNET_USER` 设置。

典型对话：“科技园站是不是太热了？能不能调一下？”→ 智能体依次调用 `data_search_objects` → `data_get_realtime` → `optimize_station_curve` → `control_draft_command(plan_ref)` → `control_safety_check` → `control_request_approval`，然后提示你去平台批准；批准后再调用 `control_request_approval` 取令牌 → `control_execute` → `control_monitor_effect`。

### 当前局限

- 平台没有登录体系：批准接口本身不鉴权，智能体身份（`--agent`/`--user`）来自配置而非认证。生产环境须在安全网关中加入 RBAC，并让批准只接受已认证的人工会话。
- 审计中没有记录模型版本（MCP 协议不提供，需要智能体框架侧传入）。
- 设计文档其余 32 个工具（运维客服、知识库、接入实施等）尚未实现。

## 目录结构

```
backend/
  app/config.py            拓扑、设备、安全边界配置（真实项目来自物模型/台账）
  app/sim/                 weather 气象 · physics 机理模型 · history 历史库 · realtime 实时引擎与报警
  app/services/            control 调控链路 · analytics 统计 · ai 预测/诊断
  app/main.py              REST + WebSocket（统一返回封套 ok/summary/data/evidence/confidence/warnings/trace_id）
  mcp_server/              MCP 工具服务（server 工具定义 · client 后端客户端 · __main__ 命令行）
  run_mcp.py               MCP 启动脚本（可用绝对路径运行）
  tests/test_api.py        机理一致性、调控链路、统计、AI 的 13 项测试
  tests/test_mcp.py        MCP 端到端 8 项测试（真实 MCP 客户端 → MCP 服务 → 后端）
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
| 智能体身份来自 MCP 启动参数 | 认证后的用户身份随请求传递，批准仅接受已认证的人工会话 |
