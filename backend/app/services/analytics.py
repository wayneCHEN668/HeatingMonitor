"""运行数据多维统计分析。

口径说明（与设计文档“能耗对标必须按度日数修正”一致）：
- 度日数 HDD = Σ max(18 − Tout_h, 0) / 24（逐时换算），单位 ℃·d
- 面积热耗   = 热量 GJ / 供热面积（展示用 GJ/万m²）
- 度日数修正单耗 = 热量 kJ / (面积 m² · HDD)，单位 kJ/(m²·℃·d)，消除天气差异后可跨时段、跨站对比
- 同比节能率 = 1 − 本期修正单耗 / 上季同期修正单耗
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from ..config import (GJ_TO_KGCE, KGCE_TO_KGCO2, SOURCE_BY_ID, SOURCES, STATION_BY_ID, STATIONS,
                      T_BALANCE, T_ROOM_TARGET)
from ..sim.history import HistoryStore, now_local

YEAR = pd.DateOffset(years=1)


def _r(x, n=2):
    if x is None:
        return None
    try:
        if np.isnan(x):
            return None
    except TypeError:
        return x
    return round(float(x), n)


def hdd(tout: pd.Series) -> float:
    return float(np.clip(T_BALANCE - tout, 0, None).sum() / 24.0)


def _in_scope(source_id: str, sources) -> bool:
    return sources is None or source_id in sources


def area_of(oid: str, sources=None) -> float:
    if oid in STATION_BY_ID:
        return STATION_BY_ID[oid].area
    if oid in SOURCE_BY_ID:
        return sum(s.area for s in STATIONS if s.source_id == oid)
    return sum(s.area for s in STATIONS if _in_scope(s.source_id, sources))


def name_of(oid: str, sources=None) -> str:
    if oid in STATION_BY_ID:
        return STATION_BY_ID[oid].name
    if oid in SOURCE_BY_ID:
        return SOURCE_BY_ID[oid].name
    if sources is not None:
        return "、".join(x.name for x in SOURCES if x.id in sources) + " 片区"
    return "全网"


class Analytics:
    def __init__(self, store: HistoryStore):
        self.store = store

    def _slice(self, oid: str, start: datetime, end: datetime, sources=None) -> pd.DataFrame:
        df = self.store.frame(oid, sources)
        return df.loc[(df.index >= start) & (df.index < end)]

    def _summary(self, oid: str, df: pd.DataFrame, sources=None) -> dict:
        if df.empty:
            return {"id": oid, "name": name_of(oid, sources), "hours": 0}
        a = area_of(oid, sources)
        h = hdd(df["tout"])
        heat = float(df["heat_gj"].sum())
        out = {
            "id": oid, "name": name_of(oid, sources), "hours": int(len(df)), "area_wm2": a / 1e4,
            "tout_avg": _r(df["tout"].mean()), "hdd": _r(h), "heat_gj": _r(heat, 1),
            "elec_kwh": _r(df["elec_kwh"].sum(), 0), "water_t": _r(df["water_t"].sum(), 1),
            "heat_mw_max": _r(df["heat_mw"].max()), "heat_mw_avg": _r(df["heat_mw"].mean()),
            "unit_heat": _r(heat / a * 1e4, 2),                       # GJ/万m²
            "norm_heat": _r(heat * 1e6 / (a * h), 1) if h > 0.5 else None,   # kJ/(m²·℃·d)
            "unit_elec": _r(df["elec_kwh"].sum() / a * 1e3, 2),       # kWh/千m²... 展示为 kWh/千m²
            "unit_water": _r(df["water_t"].sum() * 1000 / a * 1e3, 1),  # kg/千m²
        }
        if "tin" in df.columns:
            out["tin_avg"] = _r(df["tin"].mean())
            out["tin_low_hours"] = int((df["tin"] < 18).sum())
        if {"ts2", "tr2", "ts1", "tr1"} <= set(df.columns):
            out.update({"ts1_avg": _r(df["ts1"].mean(), 1), "tr1_avg": _r(df["tr1"].mean(), 1),
                        "ts2_avg": _r(df["ts2"].mean(), 1), "tr2_avg": _r(df["tr2"].mean(), 1),
                        "dt1": _r((df["ts1"] - df["tr1"]).mean(), 1), "dt2": _r((df["ts2"] - df["tr2"]).mean(), 1),
                        "valve_avg": _r(df["valve"].mean(), 1),
                        "track_err": _r((df["ts2"] - df["ts2_set"]).abs().mean(), 2)})
        if "g" in df.columns and "ts1" in df.columns:
            out.update({"ts1_avg": _r(df["ts1"].mean(), 1), "tr1_avg": _r(df["tr1"].mean(), 1),
                        "dt1": _r((df["ts1"] - df["tr1"]).mean(), 1)})
        for k in ("gas_nm3", "steam_t", "cost_yuan"):
            if k in df.columns:
                out[k] = _r(df[k].sum(), 0)
        if "cost_yuan" in df.columns and heat > 0:
            out["unit_cost"] = _r(df["cost_yuan"].sum() / heat, 1)    # 元/GJ
        if oid in SOURCE_BY_ID:
            src = SOURCE_BY_ID[oid]
            out["load_rate_max"] = _r(df["heat_mw"].max() / src.capacity_mw * 100, 1)
            out["kind"] = src.kind
            if src.kind == "gas" and df["gas_nm3"].sum() > 0:
                out["eff"] = _r(heat * 1000 / (df["gas_nm3"].sum() * 35.9) * 100, 1)
            if src.kind == "heatpump":
                out["cop_sys"] = _r(heat / 3.6 * 1000 / max(df["elec_kwh"].sum(), 1), 2)
        return out

    # ------------------------------------------------------------------
    def daily(self, day: date | None, scope: str = "NET", sources=None) -> dict:
        now = now_local()
        day = day or now.date()
        start = datetime.combine(day, datetime.min.time())
        end = start + timedelta(days=1)
        df = self._slice(scope, start, end, sources)
        if df.empty:
            raise ValueError(f"{day} 无运行数据")
        cols = [c for c in ("tout", "heat_mw", "tin", "ts1", "tr1", "ts2", "tr2", "ts2_set", "g", "g1", "valve",
                            "elec_kwh", "water_t", "gas_nm3", "steam_t", "hp_mw", "eb_mw", "tank_soc")
                if c in df.columns]
        series = {"ts": [t.strftime("%H:%M") for t in df.index],
                  **{c: [_r(v) for v in df[c].tolist()] for c in cols}}
        summ = self._summary(scope, df, sources)
        prev = self._summary(scope, self._slice(scope, start - timedelta(days=1), start, sources), sources)
        last = self._summary(scope, self._slice(scope, start - YEAR, end - YEAR, sources), sources)
        cmp = {}
        for k in ("heat_gj", "norm_heat", "elec_kwh", "water_t", "tin_avg", "tout_avg"):
            a, b, c = summ.get(k), prev.get(k), last.get(k)
            cmp[k] = {"today": a, "prev": b, "last_year": c,
                      "dod": _r((a - b) / b * 100, 1) if a is not None and b else None,
                      "yoy": _r((a - c) / c * 100, 1) if a is not None and c else None}
        # 子对象明细
        if scope == "NET":
            children = ([s.id for s in SOURCES if _in_scope(s.id, sources)]
                        + [s.id for s in STATIONS if _in_scope(s.source_id, sources)])
        elif scope in SOURCE_BY_ID:
            children = [s.id for s in STATIONS if s.source_id == scope]
        else:
            children = []
        rows = [self._summary(c, self._slice(c, start, end)) for c in children]
        peak_i = int(np.argmax(df["heat_mw"].to_numpy()))
        findings = self._daily_findings(scope, summ, prev, rows)
        return {
            "date": day.isoformat(), "scope": scope, "scope_name": name_of(scope, sources), "partial": end > now,
            "summary": summ, "compare": cmp, "series": series, "rows": rows,
            "peak": {"time": df.index[peak_i].strftime("%H:%M"), "heat_mw": _r(df["heat_mw"].iloc[peak_i])},
            "findings": findings,
        }

    def _daily_findings(self, scope, summ, prev, rows) -> list[str]:
        f = []
        if summ.get("norm_heat") and prev.get("norm_heat"):
            d = (summ["norm_heat"] - prev["norm_heat"]) / prev["norm_heat"] * 100
            f.append(f"度日数修正单耗较前一日{'上升' if d > 0 else '下降'} {abs(d):.1f}%"
                     f"（室外均温 {summ['tout_avg']}℃ vs {prev.get('tout_avg')}℃）。")
        st_rows = [r for r in rows if r["id"].startswith("ST") and r.get("hours")]
        if st_rows:
            hot = [r for r in st_rows if (r.get("tin_avg") or 0) > 21.5]
            cold = [r for r in st_rows if (r.get("tin_avg") or 99) < 18.5]
            if hot:
                f.append("室温偏高站点：" + "、".join(f"{r['name']}({r['tin_avg']}℃)" for r in hot) + "，存在过量供热。")
            if cold:
                f.append("室温偏低站点：" + "、".join(f"{r['name']}({r['tin_avg']}℃)" for r in cold) + "，需排查资用压差/阀门。")
            water = sorted(st_rows, key=lambda r: r["unit_water"] or 0, reverse=True)[0]
            med = float(np.median([r["unit_water"] or 0 for r in st_rows]))
            if water["unit_water"] and water["unit_water"] > 3 * med:
                f.append(f"{water['name']} 单位面积补水 {water['unit_water']} kg/千m²，为中位数的 "
                         f"{water['unit_water'] / med:.1f} 倍，疑似失水。")
        if not f:
            f.append("当日运行平稳，无明显异常。")
        return f

    # ------------------------------------------------------------------
    def energy(self, start: datetime, end: datetime, scope=None) -> dict:
        """节能分析：度日数修正后的单耗对标、同比节能量、过热潜力。"""
        rows = []
        for s in STATIONS:
            if not _in_scope(s.source_id, scope):
                continue
            cur = self._summary(s.id, self._slice(s.id, start, end))
            base = self._summary(s.id, self._slice(s.id, start - YEAR, end - YEAR))
            r = dict(cur)
            r["source_id"] = s.source_id
            r["base_norm_heat"] = base.get("norm_heat")
            r["base_tin"] = base.get("tin_avg")
            if cur.get("norm_heat") and base.get("norm_heat"):
                rate = 1 - cur["norm_heat"] / base["norm_heat"]
                saved = (base["norm_heat"] - cur["norm_heat"]) * s.area * cur["hdd"] / 1e6
                r.update({"saving_rate": _r(rate * 100, 1), "saved_gj": _r(saved, 1)})
            tin, tout = cur.get("tin_avg"), cur.get("tout_avg")
            if tin and tout is not None and tin > T_ROOM_TARGET:
                pot = (tin - T_ROOM_TARGET) / (tin - tout)
                r["overheat_potential"] = _r(pot * 100, 1)
                r["overheat_gj"] = _r(pot * cur["heat_gj"], 1)
            else:
                r["overheat_potential"] = 0.0
                r["overheat_gj"] = 0.0
            rows.append(r)
        norms = [r["norm_heat"] for r in rows if r.get("norm_heat")]
        med = float(np.median(norms)) if norms else None
        for r in rows:
            r["vs_median"] = _r((r["norm_heat"] / med - 1) * 100, 1) if med and r.get("norm_heat") else None
        rows.sort(key=lambda r: r.get("norm_heat") or 0, reverse=True)
        for i, r in enumerate(rows):
            r["rank"] = i + 1

        sources = []
        for src in SOURCES:
            if not _in_scope(src.id, scope):
                continue
            cur = self._summary(src.id, self._slice(src.id, start, end))
            base = self._summary(src.id, self._slice(src.id, start - YEAR, end - YEAR))
            cur["base_unit_cost"] = base.get("unit_cost")
            cur["base_norm_heat"] = base.get("norm_heat")
            sources.append(cur)

        net = self._summary("NET", self._slice("NET", start, end, scope), scope)
        net_base = self._summary("NET", self._slice("NET", start - YEAR, end - YEAR, scope), scope)
        saved = sum(r.get("saved_gj") or 0 for r in rows)
        net_out = {
            **net, "base_norm_heat": net_base.get("norm_heat"),
            "saving_rate": _r((1 - net["norm_heat"] / net_base["norm_heat"]) * 100, 1)
            if net.get("norm_heat") and net_base.get("norm_heat") else None,
            "saved_gj": _r(saved, 1), "saved_tce": _r(saved * GJ_TO_KGCE / 1000, 2),
            "saved_tco2": _r(saved * GJ_TO_KGCE * KGCE_TO_KGCO2 / 1e3, 1),   # GJ→kgce→kgCO2→t
            "overheat_gj": _r(sum(r.get("overheat_gj") or 0 for r in rows), 1),
        }
        # 逐日修正单耗趋势（本季 vs 上季同期）
        trend = []
        d = start
        while d < end:
            nd = d + timedelta(days=1)
            a = self._summary("NET", self._slice("NET", d, nd, scope), scope)
            b = self._summary("NET", self._slice("NET", d - YEAR, nd - YEAR, scope), scope)
            trend.append({"date": d.strftime("%m-%d"), "norm_heat": a.get("norm_heat"),
                          "base_norm_heat": b.get("norm_heat"), "tout": a.get("tout_avg"),
                          "base_tout": b.get("tout_avg"), "heat_gj": a.get("heat_gj")})
            d = nd
        return {"start": start.isoformat(), "end": end.isoformat(), "network": net_out, "stations": rows,
                "sources": sources, "median_norm_heat": _r(med, 1), "trend": trend,
                "method": "度日数修正单耗 = 热量(kJ) / (面积(m²) × HDD(℃·d))，HDD 基准温度 18℃；"
                          "同比基准为上一采暖季同日历区间；过热潜力按 ΔQ/Q ≈ (Tin−20)/(Tin−Tout) 估算。"}

    # ------------------------------------------------------------------
    METRIC_TEXT = {"heat_gj": ("热量", "GJ"), "elec_kwh": ("电量", "kWh"), "water_t": ("补水量", "t"),
                   "unit_heat": ("面积热耗", "GJ/万m²"), "norm_heat": ("修正单耗", "kJ/(m²·℃·d)"),
                   "tin": ("平均室温", "℃"), "cost_yuan": ("运行成本", "元")}

    def multidim(self, metric: str, level: str, grain: str, start: datetime, end: datetime, sources=None) -> dict:
        """多维透视：对象（热源/热力站）× 时间粒度（day / hour / weekday）。"""
        if metric not in self.METRIC_TEXT:
            raise ValueError("不支持的指标")
        ids = [s.id for s in SOURCES] if level == "source" else [s.id for s in STATIONS]
        if metric in ("tin",) and level == "source":
            ids = [s.id for s in STATIONS]
            level = "station"
        if metric == "cost_yuan":
            ids = [s.id for s in SOURCES]
            level = "source"
        ids = [i for i in ids if _in_scope(STATION_BY_ID[i].source_id if i in STATION_BY_ID else i, sources)]
        cols_set: list = []
        matrix = []
        totals = []
        for oid in ids:
            df = self._slice(oid, start, end).copy()
            if df.empty:
                continue
            if grain == "day":
                key = df.index.strftime("%m-%d")
            elif grain == "hour":
                key = df.index.hour
            else:
                key = df.index.dayofweek
            df["_k"] = key
            a = area_of(oid)
            g = df.groupby("_k")
            if metric in ("heat_gj", "elec_kwh", "water_t", "cost_yuan"):
                ser = g[metric].sum() if grain == "day" else g[metric].mean()
                tot = df[metric].sum()
            elif metric == "unit_heat":
                ser = g["heat_gj"].sum() / a * 1e4
                tot = df["heat_gj"].sum() / a * 1e4
            elif metric == "norm_heat":
                ser = g.apply(lambda x: x["heat_gj"].sum() * 1e6 / (a * max(hdd(x["tout"]), 0.01)))
                tot = df["heat_gj"].sum() * 1e6 / (a * max(hdd(df["tout"]), 0.01))
            else:
                ser = g["tin"].mean()
                tot = df["tin"].mean()
            if not cols_set:
                cols_set = list(ser.index)
            matrix.append({"id": oid, "name": name_of(oid), "values": [_r(ser.get(k), 2) for k in cols_set],
                           "total": _r(tot, 2), "area_wm2": a / 1e4,
                           "source_id": STATION_BY_ID[oid].source_id if oid in STATION_BY_ID else oid})
        labels = cols_set
        if grain == "hour":
            labels = [f"{int(h):02d}时" for h in cols_set]
        elif grain == "weekday":
            labels = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
            labels = [labels[int(i)] for i in cols_set]
        else:
            labels = [str(c) for c in cols_set]
        name, unit = self.METRIC_TEXT[metric]
        agg = "合计" if metric in ("heat_gj", "elec_kwh", "water_t", "cost_yuan") and grain == "day" else "均值"
        if metric in ("heat_gj", "elec_kwh", "water_t", "cost_yuan") and grain != "day":
            unit = unit + "/h"
        return {"metric": metric, "metric_name": name, "unit": unit, "level": level, "grain": grain,
                "labels": labels, "rows": matrix, "agg": agg}
