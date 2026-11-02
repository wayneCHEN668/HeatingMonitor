"""确定性气象发生器：同一时刻在历史生成与实时仿真中给出一致的室外温度。

T(t) = 季节项 + 天气过程项（多周期正弦叠加，模拟冷暖空气交替）+ 日变化 + 小扰动
预报 = 实况 + 随预见期增长的误差（随机游走，按发布时刻固定随机种子）
"""
from __future__ import annotations

import numpy as np

DAY = 86400.0
_PH = np.array([0.7, 2.1, 4.4, 1.3, 5.2])


def outdoor_temp(ts) -> np.ndarray:
    """ts: epoch 秒（标量或数组，按本地时间解释）。"""
    t = np.asarray(ts, dtype=float)
    days = t / DAY
    doy = (days + 4) % 365.2425            # 1970-01-01 为周四，近似年内日序
    hour = (t % DAY) / 3600.0 + 8          # 以 UTC+8 计
    seasonal = 4.0 - 21.0 * np.cos(2 * np.pi * (doy - 20) / 365.2425)
    synoptic = (3.0 * np.sin(2 * np.pi * days / 5.3 + _PH[0])
                + 2.2 * np.sin(2 * np.pi * days / 9.1 + _PH[1])
                + 1.5 * np.sin(2 * np.pi * days / 17.3 + _PH[2]))
    diurnal = 4.0 * np.cos(2 * np.pi * (hour - 14.5) / 24)
    small = 0.5 * np.sin(2 * np.pi * days / 0.37 + _PH[3]) + 0.3 * np.sin(2 * np.pi * days / 0.13 + _PH[4])
    return seasonal + synoptic + diurnal + small


def forecast(issue_ts: float, hours: int) -> tuple[np.ndarray, np.ndarray]:
    """返回 (未来逐时时间戳, 预报温度)。"""
    issue_hour = int(issue_ts // 3600)
    rng = np.random.default_rng(issue_hour)
    times = (issue_hour + 1 + np.arange(hours)) * 3600.0
    err = np.cumsum(rng.normal(0, 0.18, hours))
    return times, outdoor_temp(times) + err
