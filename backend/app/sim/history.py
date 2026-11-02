"""逐时历史数据生成（上一采暖季 + 本采暖季至今），供统计分析与 AI 训练使用。

真实系统中这里是时序库（TDengine / InfluxDB / TimescaleDB）的查询层。
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from ..config import (ANOMALIES, CP, GAS_LHV_MJ, PRICE, SEASON_LAST, SEASON_THIS, SOURCES,
                      STATIONS, T_BALANCE, T_DESIGN_OUT, SourceCfg)
from .physics import StationModel, build_station_model, interp_curve
from .weather import outdoor_temp

TZ = ZoneInfo("Asia/Shanghai")

# 本季已采纳 AI 优化曲线的站（距今天数），用于“调控效果评估”演示
HP_CAP = 5.0  # 热泵总容量 MW
ADOPTIONS = {"ST01": 7, "ST04": 7, "ST05": 4}


def now_local() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None)


def to_epoch(idx: pd.DatetimeIndex) -> np.ndarray:
    return idx.tz_localize(TZ).as_unit("s").asi8.astype(float)


def load_ratio(tout):
    return np.clip((T_BALANCE - np.asarray(tout)) / (T_BALANCE - T_DESIGN_OUT), 0.0, 1.3)


def elec_price(hour: np.ndarray) -> np.ndarray:
    h = np.asarray(hour)
    valley = (h >= 23) | (h < 7)
    peak = ((h >= 8) & (h < 11)) | ((h >= 17) & (h < 21))
    return np.where(valley, PRICE["elec_valley"], np.where(peak, PRICE["elec_peak"], PRICE["elec_flat"]))


def station_offset(st_id: str, base_offset: float, idx: pd.DatetimeIndex, season: str, now: datetime) -> np.ndarray:
    from ..config import STATION_BY_ID
    cfg = STATION_BY_ID[st_id]
    if season == "last":
        return np.full(len(idx), cfg.offset_last)
    off = np.full(len(idx), base_offset)
    if st_id in ADOPTIONS:
        switch = now - timedelta(days=ADOPTIONS[st_id])
        off = np.where(idx < switch, cfg.offset_last, base_offset)
    return off


def gen_station(model: StationModel, idx: pd.DatetimeIndex, tout: np.ndarray, ts1_src: np.ndarray,
                season: str, now: datetime, rng: np.random.Generator) -> pd.DataFrame:
    cfg = model.cfg
    n = len(idx)
    off = station_offset(cfg.id, cfg.offset_now, idx, season, now)
    # 外网温降：越远越低
    ts1 = ts1_src - 0.6 * cfg.dp_drop - rng.normal(0, 0.15, n)
    ts2_set = model.ideal_ts2(tout) + off
    lr = load_ratio(tout)
    dpr = model.dp_ratio(lr) * (1 + rng.normal(0, 0.02, n))
    v, sat = model.valve_for_ts2(ts1, tout, ts2_set, dpr)
    r = model.solve(ts1, tout, v, dpr)
    tin = pd.Series(r["tin"], index=idx).ewm(span=8).mean().to_numpy() + rng.normal(0, 0.12, n)

    tr1_meas = r["tr1"] + rng.normal(0, 0.15, n)
    makeup = np.clip(cfg.makeup_base * (1 + rng.normal(0, 0.2, n)), 0, None)
    an = ANOMALIES.get(cfg.id)
    if season == "this" and an:
        start = now - timedelta(days=an.get("start_days_ago", 0))
        hrs = (idx - start) / pd.Timedelta(hours=1)
        on = np.asarray(hrs >= 0)
        if an["type"] == "tr1_sensor_drift":
            tr1_meas = tr1_meas + np.where(on, an["magnitude"] * np.clip(np.asarray(hrs) / 12, 0, 1), 0)
        elif an["type"] == "makeup_leak":
            makeup = makeup + np.where(on, an["magnitude"] * (1 + rng.normal(0, 0.1, n)), 0)

    freq = np.full(n, cfg.pump_freq)
    elec = cfg.pump_kw * (freq / 50) ** 3 * 0.85 * (1 + rng.normal(0, 0.03, n)) + 3.0
    p1s = 0.9 - 0.30 * cfg.dp_drop * lr ** 2 + rng.normal(0, 0.005, n)
    p1r = 0.32 + 0.12 * cfg.dp_drop * lr ** 2 + rng.normal(0, 0.004, n)
    p2r = 0.28 + rng.normal(0, 0.004, n)
    p2s = p2r + 0.13 * (freq / 45) ** 2 + rng.normal(0, 0.004, n)
    g1 = r["g1"] * (1 + rng.normal(0, 0.01, n))
    q1 = g1 / 3.6 * CP * (ts1 - tr1_meas) / 1000
    q2 = r["g2"] / 3.6 * CP * (r["ts2"] - r["tr2"]) / 1000

    df = pd.DataFrame({
        "tout": tout, "ts1": ts1, "tr1": tr1_meas, "tr1_true": r["tr1"], "p1s": p1s, "p1r": p1r, "g1": g1,
        "ts2": r["ts2"] + rng.normal(0, 0.2, n), "tr2": r["tr2"] + rng.normal(0, 0.15, n),
        "p2s": p2s, "p2r": p2r, "g2": r["g2"] * (1 + rng.normal(0, 0.005, n)),
        "ts2_set": ts2_set, "valve": v * 100, "valve_sat": sat, "tin": tin, "pump_freq": freq,
        "heat_mw": r["heat_mw"], "heat_gj": r["heat_mw"] * 3.6, "q1_mw": q1, "q2_mw": q2,
        "elec_kwh": elec, "water_t": makeup, "curve_offset": off, "season": season,
    }, index=idx)
    df.index.name = "ts"
    return df


def gen_source(src: SourceCfg, idx: pd.DatetimeIndex, tout: np.ndarray, ts1: np.ndarray,
               st_dfs: list[pd.DataFrame], season: str, rng: np.random.Generator) -> pd.DataFrame:
    n = len(idx)
    g = sum(d["g1"].to_numpy() for d in st_dfs)
    tr1 = sum((d["g1"] * d["tr1_true"]).to_numpy() for d in st_dfs) / np.maximum(g, 1e-6) - 0.3
    heat = sum(d["heat_mw"].to_numpy() for d in st_dfs) * 1.03          # 3% 管网热损
    lr = load_ratio(tout)
    g_design = sum(d["g1"].max() for d in st_dfs)
    pump_kw = src.pump_kw * np.clip(g / g_design, 0.3, 1.2) ** 3 * 0.9
    hour = idx.hour.to_numpy()
    price = elec_price(hour)
    df = pd.DataFrame({"tout": tout, "ts1": ts1, "tr1": tr1, "g": g,
                       "p_s": src.p_supply - 0.05 * lr + rng.normal(0, 0.004, n),
                       "p_r": src.p_return + rng.normal(0, 0.003, n),
                       "heat_mw": heat, "heat_gj": heat * 3.6}, index=idx)
    gas = np.zeros(n); steam = np.zeros(n); prod_elec = np.zeros(n); soc = np.zeros(n)
    hp_mw = np.zeros(n); eb_mw = np.zeros(n); dis_mw = np.zeros(n)
    if src.kind == "chp":
        steam = df["heat_gj"].to_numpy() / 2.30 * 1.01
        fuel_cost = df["heat_gj"].to_numpy() * PRICE["steam_gj"]
    elif src.kind == "gas":
        cap_each = 14.0
        part = heat / (np.ceil(np.maximum(heat, 0.1) / (cap_each * 0.92)) * cap_each)
        eta = 0.93 - 0.06 * np.abs(part - 0.75)
        gas = df["heat_gj"].to_numpy() * 1000 / (eta * GAS_LHV_MJ)
        fuel_cost = gas * PRICE["gas"]
    else:  # 热泵 + 电极锅炉 + 蓄热罐
        cap = 70.0  # MWh
        s = cap * 0.4
        for i in range(n):
            d = heat[i]
            hp = min(d, HP_CAP)
            rest = d - hp
            if hour[i] >= 23 or hour[i] < 7:      # 谷电蓄热
                eb = min(10.0, rest + (cap - s))
                s = min(cap, s + (eb - rest))
                dis = 0.0
            else:
                dis = min(rest, s)
                s -= dis
                eb = rest - dis
            hp_mw[i], eb_mw[i], dis_mw[i], soc[i] = hp, eb, dis, s / cap * 100
        cop = 4.3 - 0.03 * (ts1 - 60)
        prod_elec = hp_mw * 1000 / cop + eb_mw * 1000 / 0.99
        fuel_cost = prod_elec * price
    elec = pump_kw + 40 + prod_elec
    water = np.clip(2.0 * (1 + rng.normal(0, 0.15, n)), 0, None)
    df["elec_kwh"] = elec
    df["water_t"] = water
    df["gas_nm3"] = gas
    df["steam_t"] = steam
    df["hp_mw"] = hp_mw
    df["eb_mw"] = eb_mw
    df["tank_dis_mw"] = dis_mw
    df["tank_soc"] = soc
    df["cost_yuan"] = fuel_cost + (pump_kw + 40) * price + water * PRICE["water"]
    df["season"] = season
    df.index.name = "ts"
    return df


class HistoryStore:
    """内存时序库。stations/sources: {id: DataFrame(逐时)}。"""

    def __init__(self, seed: int = 42):
        self.models: dict[str, StationModel] = {c.id: build_station_model(c) for c in STATIONS}
        self.stations: dict[str, pd.DataFrame] = {}
        self.sources: dict[str, pd.DataFrame] = {}
        self.generate(seed)

    def generate(self, seed: int):
        now = now_local()
        end_this = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
        ranges = {
            "last": pd.date_range(SEASON_LAST[0], SEASON_LAST[1], freq="h"),
            "this": pd.date_range(min(SEASON_THIS[0], end_this - timedelta(days=14)), end_this, freq="h"),
        }
        parts_st: dict[str, list] = {c.id: [] for c in STATIONS}
        parts_src: dict[str, list] = {s.id: [] for s in SOURCES}
        for season, idx in ranges.items():
            rng = np.random.default_rng(seed + (0 if season == "last" else 1))
            tout = outdoor_temp(to_epoch(idx))
            for src in SOURCES:
                ts1 = interp_curve(tout, src.curve) + rng.normal(0, 0.4, len(idx))
                dfs = []
                for cfg in STATIONS:
                    if cfg.source_id != src.id:
                        continue
                    d = gen_station(self.models[cfg.id], idx, tout, ts1, season, now, rng)
                    parts_st[cfg.id].append(d)
                    dfs.append(d)
                parts_src[src.id].append(gen_source(src, idx, tout, ts1, dfs, season, rng))
        self.stations = {k: pd.concat(v) for k, v in parts_st.items()}
        self.sources = {k: pd.concat(v) for k, v in parts_src.items()}

    # ---------- 查询 ----------
    def frame(self, object_id: str) -> pd.DataFrame:
        if object_id in self.stations:
            return self.stations[object_id]
        if object_id in self.sources:
            return self.sources[object_id]
        if object_id == "NET":
            return self.network()
        raise KeyError(object_id)

    def network(self) -> pd.DataFrame:
        srcs = list(self.sources.values())
        base = srcs[0][["tout", "season"]].copy()
        for col in ("heat_mw", "heat_gj", "elec_kwh", "water_t", "gas_nm3", "steam_t", "cost_yuan", "g"):
            base[col] = sum(s[col] for s in srcs)
        st = list(self.stations.values())
        base["elec_kwh"] += sum(s["elec_kwh"] for s in st)
        base["water_t"] += sum(s["water_t"] for s in st)
        base["tin"] = sum(s["tin"] for s in st) / len(st)
        return base

    def append_hour(self, object_id: str, ts: datetime, row: dict):
        store = self.stations if object_id in self.stations else self.sources
        df = store[object_id]
        if ts in df.index:
            return
        new = pd.DataFrame([row], index=pd.DatetimeIndex([ts], name="ts"))
        store[object_id] = pd.concat([df, new.reindex(columns=df.columns)])

    @staticmethod
    def hdd(tout: pd.Series) -> float:
        """度日数（逐时数据换算）。"""
        return float(np.clip(T_BALANCE - tout, 0, None).sum() / 24.0)


def safe(v):
    if v is None:
        return None
    if isinstance(v, (float, np.floating)):
        return None if math.isnan(v) else round(float(v), 3)
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.bool_,)):
        return bool(v)
    return v
