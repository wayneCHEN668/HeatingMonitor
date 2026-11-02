"""气象实况与预报（对应设计文档 data_get_weather）。

仿真气象源只有气温；风速、日照等字段在接入真实气象服务后补充。
寒潮判定参照 GB/T 21987《寒潮等级》思路的简化版：48 小时内日最低气温下降 ≥ 8℃ 且最低气温 ≤ 4℃。
"""
from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd

from ..sim.history import HistoryStore, now_local, to_epoch
from ..sim.weather import forecast, outdoor_temp


def _r(v, n=1):
    return None if v is None or (isinstance(v, float) and np.isnan(v)) else round(float(v), n)


def weather_report(store: HistoryStore, hours_ahead: int = 72) -> dict:
    hours_ahead = int(min(max(hours_ahead, 6), 168))
    now = now_local()
    cur = float(outdoor_temp(to_epoch(pd.DatetimeIndex([now]))[0]))
    obs = store.frame("NET")["tout"]
    obs = obs[obs.index >= now - timedelta(hours=24)]
    issue = now.replace(minute=0, second=0, microsecond=0)
    _, tf = forecast(float(to_epoch(pd.DatetimeIndex([issue]))[0]), hours_ahead)
    fidx = pd.date_range(issue + timedelta(hours=1), periods=hours_ahead, freq="h")
    fs = pd.Series(tf, index=fidx)

    days = []
    for d, g in fs.groupby(fs.index.date):
        days.append({"date": d.strftime("%m-%d"), "min": _r(g.min()), "max": _r(g.max()), "mean": _r(g.mean()),
                     "hours": int(len(g))})
    warnings = []
    obs_min = float(obs.min()) if len(obs) else cur
    # 寒潮：未来 48h 内最低气温相对近 24h 最低气温下降 ≥ 8℃，且降到 4℃ 以下
    f48 = fs.iloc[:48]
    drop = obs_min - float(f48.min())
    cold_wave = drop >= 8 and f48.min() <= 4
    if cold_wave:
        warnings.append(f"寒潮预警：{f48.idxmin():%m-%d %H时} 前后最低 {f48.min():.1f}℃，较近 24h 最低下降 {drop:.1f}℃")
    elif drop >= 5:
        warnings.append(f"较大降温：未来 48h 最低 {f48.min():.1f}℃，较近 24h 最低下降 {drop:.1f}℃，建议提前预升温")
    i_min = int(np.argmin(tf))
    summary = (f"当前室外 {cur:.1f}℃；未来 {hours_ahead}h 最低 {tf.min():.1f}℃（{fidx[i_min]:%m-%d %H时}）、"
               f"最高 {tf.max():.1f}℃" + (f"；{warnings[0]}" if warnings else ""))
    return {
        "summary": summary,
        "data": {
            "location": "示范城市（单一气象站）", "now": now.isoformat(timespec="seconds"), "current_tout": _r(cur),
            "observed_24h": {"min": _r(obs.min()), "max": _r(obs.max()), "mean": _r(obs.mean())},
            "forecast_daily": days,
            "forecast_hourly": [{"ts": t.strftime("%m-%d %H:%M"), "tout": _r(v)} for t, v in fs.items()],
            "cold_wave": bool(cold_wave), "drop_48h": _r(drop),
            "fields_unavailable": ["风速", "日照", "湿度"],
        },
        "warnings": warnings,
        "evidence": ["仿真气象源（气温）；预报误差随预见期增大"],
    }
