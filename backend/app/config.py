"""热网拓扑与设备参数配置。

真实项目中这部分来自物模型/台账库；这里用静态配置驱动仿真器。
所有对象使用统一 ID（HS = 热源，ST = 热力站），指标使用标准名（见 METRICS）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

CP = 4.186                 # 水的比热 kJ/(kg·K)
T_DESIGN_OUT = -24.0       # 供暖室外计算温度 ℃（严寒地区）
T_ROOM_TARGET = 20.0       # 室温目标 ℃
T_BALANCE = 18.0           # 度日数基准温度 ℃
CURVE_TOUT = [-26, -20, -15, -10, -5, 0, 5, 10]   # 气候补偿曲线横坐标（室外温度）

# 采暖季：本季从 10-20 开始；上一季用于同比基准
SEASON_THIS = (datetime(2026, 10, 20), None)
SEASON_LAST = (datetime(2025, 10, 20), datetime(2026, 4, 10))

# 能源折算
GJ_TO_KGCE = 34.12         # 1 GJ 热量 ≈ 34.12 kgce
KGCE_TO_KGCO2 = 2.66
GAS_LHV_MJ = 35.9          # 天然气低位热值 MJ/Nm³
PRICE = {"gas": 3.2, "steam_gj": 42.0, "elec_peak": 1.05, "elec_flat": 0.68, "elec_valley": 0.32, "water": 6.5}


@dataclass
class SourceCfg:
    id: str
    name: str
    kind: str                        # chp | gas | heatpump
    capacity_mw: float
    curve: list[tuple[float, float]]  # 一次供水温度曲线：室外温度 -> 供温
    p_supply: float                  # 出口供水压力 MPa
    p_return: float                  # 回水压力 MPa
    pump_kw: float                   # 循环泵额定总功率
    xy: tuple[float, float]          # 拓扑图坐标
    units: list[dict] = field(default_factory=list)  # 主要设备


@dataclass
class StationCfg:
    id: str
    name: str
    source_id: str
    area: float                 # 供热面积 m²
    design_index: float         # 设计热指标 W/m²
    system: str                 # radiator 散热器 | floor 地暖
    dp_drop: float              # 远端压差衰减系数（越大越容易“远冷”）
    building_factor: float      # 实际围护结构散热系数 / 设计值
    offset_last: float          # 上一采暖季曲线相对理想曲线偏移 ℃
    offset_now: float           # 本季当前曲线偏移 ℃
    pump_kw: float              # 二次循环泵额定功率
    pump_freq: float            # 二次泵运行频率 Hz
    makeup_base: float          # 正常补水量 t/h
    xy: tuple[float, float]
    autonomy: str = "L2"        # 自治等级 L1-L4
    address: str = ""


SOURCES: list[SourceCfg] = [
    SourceCfg(
        "HS01", "东郊热电厂首站", "chp", 66.0,
        [(-26, 118), (-24, 115), (-10, 96), (0, 82), (5, 74), (10, 66)],
        1.05, 0.30, 900, (120, 210),
        units=[
            {"id": "HX1", "name": "1#汽水换热器", "type": "hx", "cap_mw": 22},
            {"id": "HX2", "name": "2#汽水换热器", "type": "hx", "cap_mw": 22},
            {"id": "HX3", "name": "3#汽水换热器", "type": "hx", "cap_mw": 22},
            {"id": "P1", "name": "1#循环泵", "type": "pump"},
            {"id": "P2", "name": "2#循环泵", "type": "pump"},
            {"id": "P3", "name": "3#循环泵", "type": "pump"},
            {"id": "P4", "name": "4#循环泵(备)", "type": "pump"},
        ],
    ),
    SourceCfg(
        "HS02", "城北燃气调峰锅炉房", "gas", 42.0,
        [(-26, 112), (-24, 110), (-10, 92), (0, 80), (5, 72), (10, 65)],
        0.95, 0.28, 420, (520, 90),
        units=[
            {"id": "B1", "name": "1#燃气锅炉", "type": "boiler", "cap_mw": 14},
            {"id": "B2", "name": "2#燃气锅炉", "type": "boiler", "cap_mw": 14},
            {"id": "B3", "name": "3#燃气锅炉", "type": "boiler", "cap_mw": 14},
            {"id": "P1", "name": "1#循环泵", "type": "pump"},
            {"id": "P2", "name": "2#循环泵", "type": "pump"},
            {"id": "P3", "name": "3#循环泵(备)", "type": "pump"},
        ],
    ),
    SourceCfg(
        "HS03", "南区热泵能源站", "heatpump", 18.0,
        [(-26, 95), (-24, 92), (-10, 80), (0, 70), (5, 64), (10, 58)],
        0.85, 0.30, 220, (560, 400),
        units=[
            {"id": "HP1", "name": "1#污水源热泵", "type": "heatpump", "cap_mw": 2.5},
            {"id": "HP2", "name": "2#污水源热泵", "type": "heatpump", "cap_mw": 2.5},
            {"id": "EB1", "name": "电极锅炉", "type": "eboiler", "cap_mw": 10},
            {"id": "TK1", "name": "蓄热罐 6000m³", "type": "tank"},
            {"id": "P1", "name": "1#循环泵", "type": "pump"},
            {"id": "P2", "name": "2#循环泵", "type": "pump"},
        ],
    ),
]

S = StationCfg
STATIONS: list[StationCfg] = [
    S("ST01", "锦绣花园站", "HS01", 150000, 48, "radiator", 0.10, 1.00, 3.0, 0.5, 45, 44, 0.12, (240, 120), "L3", "东湖路 18 号"),
    S("ST02", "东湖一号站", "HS01", 200000, 50, "radiator", 0.15, 0.95, 3.5, 3.0, 55, 46, 0.15, (260, 300), "L2", "东湖路 66 号"),
    S("ST03", "东湖三号站", "HS01", 120000, 52, "radiator", 0.25, 1.02, 3.0, 1.0, 37, 45, 0.10, (330, 210), "L2", "望湖街 3 号"),
    S("ST04", "滨河新城站", "HS01", 180000, 42, "floor", 0.30, 0.98, 2.5, 0.0, 45, 43, 0.12, (380, 330), "L2", "滨河大道 101 号"),
    S("ST05", "望江苑站", "HS01", 100000, 50, "radiator", 0.35, 1.00, 3.0, 0.5, 30, 44, 0.08, (410, 150), "L2", "望江路 9 号"),
    S("ST06", "体育中心站", "HS01", 80000, 55, "radiator", 0.20, 1.05, 2.0, 1.5, 30, 45, 0.06, (200, 360), "L1", "体育馆路 1 号"),
    S("ST07", "北苑小区站", "HS02", 140000, 52, "radiator", 0.20, 1.00, 3.0, 1.0, 37, 45, 0.12, (640, 40), "L2", "北苑路 22 号"),
    S("ST08", "文化路站", "HS02", 90000, 50, "radiator", 0.25, 1.00, 3.0, 1.0, 30, 44, 0.08, (380, 70), "L2", "文化路 55 号"),
    S("ST09", "科技园站", "HS02", 160000, 45, "floor", 0.30, 0.92, 3.0, 2.5, 45, 45, 0.12, (690, 170), "L2", "科技三路 8 号"),
    S("ST10", "北站家园站", "HS02", 110000, 55, "radiator", 0.80, 1.18, 2.5, 1.0, 37, 47, 0.10, (760, 70), "L2", "站前街 12 号"),
    S("ST11", "南湖湾站", "HS03", 70000, 45, "floor", 0.15, 1.00, 2.5, 0.5, 22, 43, 0.06, (680, 330), "L2", "南湖路 6 号"),
    S("ST12", "绿城站", "HS03", 90000, 48, "radiator", 0.25, 1.00, 3.0, 1.0, 30, 44, 0.08, (700, 460), "L2", "绿城大道 77 号"),
    S("ST13", "学府里站", "HS03", 60000, 50, "radiator", 0.35, 1.03, 3.0, 1.0, 22, 44, 0.05, (440, 450), "L2", "学府路 30 号"),
]

SOURCE_BY_ID = {s.id: s for s in SOURCES}
STATION_BY_ID = {s.id: s for s in STATIONS}

# 注入的故障场景（用于异常检测演示），时间以“距今天数”给出
ANOMALIES = {
    "ST03": {"type": "tr1_sensor_drift", "start_days_ago": 5, "magnitude": 6.0},
    "ST07": {"type": "makeup_leak", "start_days_ago": 3, "magnitude": 1.4},
    "ST12": {"type": "comm_intermittent"},
}

# 安全边界（调控链路使用）
LIMITS = {
    "radiator": {"ts2": (28.0, 75.0)},
    "floor": {"ts2": (25.0, 50.0)},
    "valve": (5.0, 100.0),
    "valve_step": 20.0,          # 单次阀门最大调整 %
    "ts2_step": 5.0,             # 单次供温设定最大调整 ℃
    "curve_point_step": 8.0,     # 曲线单点最大调整 ℃
    "curve_point_warn": 4.0,
    "freq": (30.0, 50.0),
    "freq_step": 5.0,
    "max_cmds_per_hour": 6,
    "freeze_tout": -10.0,        # 室外低于该温度时阀门不得低于 freeze_valve
    "freeze_valve": 15.0,
    "tin_block": 16.0,
    "tin_warn": (18.0, 24.0),
}

METRICS = {
    "tout": ("室外温度", "℃"), "ts1": ("一次供水温度", "℃"), "tr1": ("一次回水温度", "℃"),
    "p1s": ("一次供水压力", "MPa"), "p1r": ("一次回水压力", "MPa"), "g1": ("一次流量", "t/h"),
    "ts2": ("二次供水温度", "℃"), "tr2": ("二次回水温度", "℃"), "p2s": ("二次供水压力", "MPa"),
    "p2r": ("二次回水压力", "MPa"), "g2": ("二次流量", "t/h"), "ts2_set": ("二次供温设定", "℃"),
    "valve": ("调节阀开度", "%"), "tin": ("典型室温", "℃"), "heat_mw": ("热功率", "MW"),
    "heat_gj": ("热量", "GJ"), "elec_kwh": ("电量", "kWh"), "water_t": ("补水量", "t"),
    "pump_freq": ("循环泵频率", "Hz"),
}
