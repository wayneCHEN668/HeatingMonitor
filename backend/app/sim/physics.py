"""热力站稳态机理模型（numpy 向量化）。

模型：一次侧 —— 板式换热器（逆流 ε-NTU）—— 二次侧 —— 建筑（散热器 + 围护结构）
    Q   = E·(Ts1 − Tr2)               E = ε·Cmin
    Q   = k1·(Tm − Tin)               Tm = (Ts2 + Tr2)/2 = Tr2 + Q/(2·C2)
    Q   = k2·(Tin − Tout)
三式联立可得 Q 的闭式解，其余温度依次求出。所有输入可为标量或等长数组。
单位：Q kW，C kW/K，G kg/s，温度 ℃。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..config import CP, CURVE_TOUT, SOURCE_BY_ID, T_DESIGN_OUT, T_ROOM_TARGET, StationCfg

VALVE_R = 30.0  # 等百分比阀门可调比


def interp_curve(tout, pts):
    xs = np.array([p[0] for p in pts], dtype=float)
    ys = np.array([p[1] for p in pts], dtype=float)
    return np.interp(tout, xs, ys)


def eps_counterflow(ua, c1, c2):
    cmin = np.minimum(c1, c2)
    cmax = np.maximum(c1, c2)
    cr = np.where(cmax > 0, cmin / np.maximum(cmax, 1e-9), 0.0)
    ntu = ua / np.maximum(cmin, 1e-9)
    near1 = np.abs(1 - cr) < 1e-6
    e = np.exp(-ntu * (1 - cr))
    eps = np.where(near1, ntu / (1 + ntu), (1 - e) / np.maximum(1 - cr * e, 1e-12))
    return np.clip(eps, 0.0, 0.999), cmin


def valve_fraction(v):
    """阀门开度(0-1) -> 相对流量（等百分比特性）。"""
    v = np.clip(v, 0.0, 1.0)
    return (VALVE_R ** (v - 1) - 1 / VALVE_R) / (1 - 1 / VALVE_R)


@dataclass
class StationModel:
    cfg: StationCfg
    q_design: float      # kW
    k1: float            # 散热器 kW/K
    k2_design: float     # 建筑 kW/K（设计）
    c2_design: float     # 二次侧水当量 kW/K（额定频率）
    ua: float            # 换热器 UA kW/K
    g1_max: float        # 阀全开设计流量 kg/s
    ts2_design: float
    tr2_design: float

    @property
    def k2(self) -> float:
        return self.k2_design * self.cfg.building_factor

    def c2(self, freq=None):
        f = self.cfg.pump_freq if freq is None else freq
        return self.c2_design * (np.asarray(f, dtype=float) / self.cfg.pump_freq)

    # ---------- 稳态求解 ----------
    def solve(self, ts1, tout, valve, dp_ratio=1.0, freq=None):
        """给定一次供温、室外温度、阀门开度(0-1)、可用压差比，求稳态工况。"""
        ts1 = np.asarray(ts1, dtype=float)
        tout = np.asarray(tout, dtype=float)
        g1 = self.g1_max * valve_fraction(valve) * np.sqrt(np.clip(dp_ratio, 0.05, 1.5))
        c1 = np.maximum(g1 * CP, 1e-6)
        c2 = self.c2(freq)
        eps, cmin = eps_counterflow(self.ua, c1, c2)
        E = np.maximum(eps * cmin, 1e-6)
        k1, k2 = self.k1, self.k2
        q = k1 * (ts1 - tout) / (1 + k1 / E - k1 / (2 * c2) + k1 / k2)
        q = np.maximum(q, 0.0)
        tr2 = ts1 - q / E
        tin = tout + q / k2
        ts2 = tr2 + q / c2
        tr1 = ts1 - q / c1
        return {
            "g1": g1 * 3.6,                     # t/h
            "g2": c2 / CP * 3.6,                # t/h
            "heat_mw": q / 1000.0,
            "ts2": ts2, "tr2": tr2, "tr1": tr1, "tin": tin,
        }

    def valve_for_ts2(self, ts1, tout, ts2_set, dp_ratio=1.0, freq=None, iters=28):
        """二分法反求达到二次供温设定所需的阀门开度，返回 (开度0-1, 是否饱和)。"""
        ts1 = np.asarray(ts1, dtype=float)
        lo = np.zeros_like(ts1)
        hi = np.ones_like(ts1)
        for _ in range(iters):
            mid = (lo + hi) / 2
            ts2 = self.solve(ts1, tout, mid, dp_ratio, freq)["ts2"]
            too_hot = ts2 > ts2_set
            hi = np.where(too_hot, mid, hi)
            lo = np.where(too_hot, lo, mid)
        v = (lo + hi) / 2
        saturated = v > 0.995
        return v, saturated

    # ---------- 曲线 ----------
    def ideal_ts2(self, tout, tin=T_ROOM_TARGET):
        """按设计参数，使室温达到 tin 所需的二次供水温度（质调节理想曲线）。"""
        tout = np.asarray(tout, dtype=float)
        q = np.maximum(self.k2_design * (tin - tout), 0)
        c2 = self.c2()
        tm = tin + q / self.k1
        return np.maximum(tm + q / (2 * c2), 25.0)

    def curve_with_offset(self, offset: float) -> list[list[float]]:
        return [[float(t), round(float(self.ideal_ts2(t)) + offset, 1)] for t in CURVE_TOUT]

    def dp_ratio(self, load_ratio):
        lr = np.clip(load_ratio, 0, 1.3)
        return np.clip(1 - self.cfg.dp_drop * lr ** 2, 0.15, 1.2)


def _solve_ua(target_eps, c1, c2):
    lo, hi = 1e-3, 1e6
    for _ in range(200):
        mid = np.sqrt(lo * hi)
        eps, _ = eps_counterflow(mid, c1, c2)
        if eps < target_eps:
            lo = mid
        else:
            hi = mid
    return float(np.sqrt(lo * hi))


def build_station_model(cfg: StationCfg) -> StationModel:
    src = SOURCE_BY_ID[cfg.source_id]
    qd = cfg.area * cfg.design_index / 1000.0
    ts2d, tr2d = (62.0, 45.0) if cfg.system == "radiator" else (45.0, 35.0)
    k2 = qd / (T_ROOM_TARGET - T_DESIGN_OUT)
    c2 = qd / (ts2d - tr2d)
    k1 = qd / ((ts2d + tr2d) / 2 - T_ROOM_TARGET)
    ts1d = float(interp_curve(T_DESIGN_OUT, src.curve))
    tr1d = 50.0 if cfg.system == "radiator" else 42.0
    c1d = qd / (ts1d - tr1d)
    eps_d, cmin = eps_counterflow(1.0, c1d, c2)  # 仅为取 cmin
    cmin = min(c1d, c2)
    target_eps = qd / (cmin * (ts1d - tr2d))
    ua = _solve_ua(min(target_eps, 0.98), c1d, c2)
    g1_design = c1d / CP
    # 设计工况下阀门约开 75%
    g1_max = g1_design / float(valve_fraction(0.75))
    return StationModel(cfg, qd, k1, k2, c2, ua, g1_max, ts2d, tr2d)
