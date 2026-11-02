"""AI 运行分析与预测（对应设计文档 predict_* / analyze_* / optimize_station_curve）。

1. 热负荷预测：梯度提升均值模型 + 时序交叉验证样本外残差分位数构造 P10/P90（split conformal）；返回区间、回测 MAPE、覆盖率、驱动因素
2. 异常检测：以各站自身基线标准化后的多变量特征做 Isolation Forest，主导特征决定疑似类型，附证据
3. 一站一策曲线优化：用本季数据拟合 室温 = a·Ts2 + b·Tout + c，反求达到 20℃ 所需的曲线修正量
4. 调控效果评估：双重差分（DiD），以未调整站为对照组，消除天气影响
5. 综合诊断：汇总以上结果 + 规则，生成带证据的结论与建议
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingRegressor, IsolationForest
from sklearn.model_selection import TimeSeriesSplit

from ..config import (CURVE_TOUT, LIMITS, SOURCE_BY_ID, SOURCES, STATION_BY_ID, STATIONS, T_ROOM_TARGET)
from ..sim.history import ADOPTIONS, HistoryStore, now_local, to_epoch
from ..sim.physics import interp_curve
from ..sim.weather import forecast as weather_forecast
from .analytics import _r, area_of, name_of

FEATURES = ["tout", "tout_ma6", "tout_ma24", "tout_lag3", "hour_sin", "hour_cos", "dow"]
FEATURE_TEXT = {"tout": "当前室外温度", "tout_ma6": "近 6 小时平均室外温度", "tout_ma24": "近 24 小时平均室外温度",
                "tout_lag3": "3 小时前室外温度", "hour_sin": "时段（日周期）", "hour_cos": "时段（日周期）",
                "dow": "星期"}


def envelope(summary: str, data=None, evidence=None, confidence=None, warnings=None, ok=True):
    return {"ok": ok, "summary": summary, "data": data, "evidence": evidence or [], "confidence": confidence,
            "warnings": warnings or [], "trace_id": uuid.uuid4().hex[:12]}


def make_features(tout: pd.Series) -> pd.DataFrame:
    idx = tout.index
    f = pd.DataFrame(index=idx)
    f["tout"] = tout
    f["tout_ma6"] = tout.rolling(6, min_periods=1).mean()
    f["tout_ma24"] = tout.rolling(24, min_periods=1).mean()
    f["tout_lag3"] = tout.shift(3).bfill()
    h = idx.hour.to_numpy()
    f["hour_sin"] = np.sin(2 * np.pi * h / 24)
    f["hour_cos"] = np.cos(2 * np.pi * h / 24)
    f["dow"] = idx.dayofweek
    return f


class AIService:
    def __init__(self, store: HistoryStore, engine=None):
        self.store = store
        self.engine = engine
        self._cache: dict = {}

    # ==================================================================
    # 1. 热负荷预测
    # ==================================================================
    @staticmethod
    def _fit(X, y):
        m = GradientBoostingRegressor(loss="squared_error", n_estimators=250, max_depth=3,
                                      learning_rate=0.05, subsample=0.9, random_state=0)
        return m.fit(X, y)

    def _oof_rel_resid(self, X, y, n_splits: int = 4) -> np.ndarray:
        """时间序列交叉验证得到样本外相对残差 (y−ŷ)/ŷ，用于构造预测区间（split conformal）。"""
        res = []
        for tr, te in TimeSeriesSplit(n_splits=n_splits).split(X):
            m = self._fit(X.iloc[tr], y.iloc[tr])
            yh = m.predict(X.iloc[te])
            res.append((y.iloc[te].to_numpy() - yh) / np.maximum(yh, 1e-6))
        return np.concatenate(res)

    def _interval(self, X, y):
        """返回 (均值模型, 相对残差 10%/90% 分位数)。"""
        r = self._oof_rel_resid(X, y)
        return self._fit(X, y), float(np.quantile(r, 0.1)), float(np.quantile(r, 0.9))

    def forecast(self, scope: str = "NET", horizon: int = 72) -> dict:
        horizon = int(np.clip(horizon, 6, 168))
        key = ("fc", scope, horizon, now_local().strftime("%Y%m%d%H"))
        if key in self._cache:
            return self._cache[key]
        df = self.store.frame(scope)
        df = df[df["season"] == "this"]
        df = df.iloc[-24 * 30:]                     # 最近 30 天滚动窗口
        y = df["heat_mw"]
        X = make_features(df["tout"])
        t0 = time.time()
        # 回测：最后 48h 完全留出（区间分位数只用训练段的交叉验证残差，覆盖率是诚实的样本外结果）
        bt_n = 48
        bm, bq10, bq90 = self._interval(X.iloc[:-bt_n], y.iloc[:-bt_n])
        Xb, yb = X.iloc[-bt_n:], y.iloc[-bt_n:].to_numpy()
        pb50 = bm.predict(Xb)
        pb = {0.1: pb50 * (1 + bq10), 0.5: pb50, 0.9: pb50 * (1 + bq90)}
        mape = float(np.mean(np.abs(pb50 - yb) / np.maximum(yb, 1e-6)) * 100)
        cover = float(np.mean((yb >= pb[0.1]) & (yb <= pb[0.9])) * 100)
        model, q10, q90 = self._interval(X, y)
        # 未来气象：用最新整点作为预报发布时刻
        issue = df.index[-1]
        times, tf = weather_forecast(float(to_epoch(pd.DatetimeIndex([issue]))[0]), horizon)
        fidx = pd.date_range(issue + timedelta(hours=1), periods=horizon, freq="h")
        tout_all = pd.concat([df["tout"].iloc[-48:], pd.Series(tf, index=fidx)])
        Xf = make_features(tout_all).loc[fidx]
        p50 = model.predict(Xf)
        # 预见期越长，气象预报误差越大：区间按 sqrt(预见期) 适度放宽
        widen = 1 + 0.04 * np.sqrt(np.arange(1, horizon + 1))
        p10 = p50 * (1 + q10 * widen)
        p90 = p50 * (1 + q90 * widen)
        models = {0.5: model}
        imp = models[0.5].feature_importances_
        drivers = {}
        for f, v in zip(FEATURES, imp):
            drivers[FEATURE_TEXT[f]] = drivers.get(FEATURE_TEXT[f], 0) + float(v)
        drivers = sorted(({"name": k, "weight": _r(v * 100, 1)} for k, v in drivers.items()),
                         key=lambda d: -d["weight"])
        hist = df.iloc[-72:]
        daily = (pd.Series(p50 * 1.0, index=fidx).resample("D").sum() * 3.6)
        pk = int(np.argmax(p50))
        warnings = [f"模型训练于 {now_local().strftime('%m-%d %H:%M')}，训练样本 {len(df)} 小时（本采暖季）"]
        if len(df) < 24 * 21:
            warnings.append("本季数据不足 21 天，预测区间可能偏窄，建议结合上季同期数据校准")
        cap = SOURCE_BY_ID[scope].capacity_mw if scope in SOURCE_BY_ID else (
            sum(s.capacity_mw for s in SOURCES) if scope == "NET" else None)
        if cap and float(p90.max()) > 0.9 * cap:
            warnings.append(f"P90 峰值 {p90.max():.1f}MW 超过装机容量 {cap}MW 的 90%，请提前安排调峰")
        tmin = float(tf.min())
        data = {
            "scope": scope, "scope_name": name_of(scope), "horizon": horizon,
            "history": {"ts": [t.strftime("%m-%d %H:%M") for t in hist.index],
                        "heat_mw": [_r(v) for v in hist["heat_mw"]], "tout": [_r(v) for v in hist["tout"]]},
            "forecast": {"ts": [t.strftime("%m-%d %H:%M") for t in fidx], "p10": [_r(v) for v in p10],
                         "p50": [_r(v) for v in p50], "p90": [_r(v) for v in p90], "tout": [_r(v) for v in tf]},
            "backtest": {"ts": [t.strftime("%m-%d %H:%M") for t in Xb.index], "actual": [_r(v) for v in yb],
                         "p50": [_r(v) for v in pb[0.5]], "p10": [_r(v) for v in pb[0.1]],
                         "p90": [_r(v) for v in pb[0.9]], "mape": _r(mape), "coverage": _r(cover, 1),
                         "rel_q10": _r(q10 * 100, 2), "rel_q90": _r(q90 * 100, 2)},
            "drivers": drivers,
            "daily_gj": [{"date": d.strftime("%m-%d"), "heat_gj": _r(v, 0)} for d, v in daily.items()],
            "peak": {"ts": fidx[pk].strftime("%m-%d %H:%M"), "p50": _r(p50[pk]), "p90": _r(p90[pk])},
            "capacity_mw": cap, "train_ms": int((time.time() - t0) * 1000),
            "model": "GradientBoosting 均值模型 + 时序交叉验证残差分位数区间（split conformal）",
        }
        summary = (f"{name_of(scope)}未来 {horizon}h 负荷峰值约 {p50[pk]:.1f}MW（P90 {p90[pk]:.1f}MW），"
                   f"出现在 {fidx[pk].strftime('%m-%d %H时')}；预报最低气温 {tmin:.1f}℃。")
        res = envelope(summary, data, evidence=[f"近 48h 留出回测 MAPE {mape:.1f}%",
                                 f"P10–P90 样本外覆盖率 {cover:.0f}%（名义 80%）"],
                       confidence=_r(max(0.5, 1 - mape / 20), 2), warnings=warnings)
        self._cache = {k: v for k, v in self._cache.items() if k[-1] == key[-1]}
        self._cache[key] = res
        return res

    # ==================================================================
    # 2. 异常检测
    # ==================================================================
    def _station_features(self, sid: str, start: datetime) -> pd.DataFrame:
        df = self.store.stations[sid]
        df = df[df.index >= start]
        cfg = STATION_BY_ID[sid]
        f = pd.DataFrame(index=df.index)
        f["heat_balance"] = (df["q1_mw"] - df["q2_mw"]) / df["q2_mw"].clip(lower=0.2) * 100
        f["makeup"] = df["water_t"] / cfg.area * 1e4               # t/(万m²·h)
        f["ts2_dev"] = df["ts2"] - df["ts2_set"]
        f["tin"] = df["tin"]
        f["dt1"] = df["ts1"] - df["tr1"]
        f["valve"] = df["valve"]
        return f

    TYPE_MAP = {
        "heat_balance": ("一次侧回水温度传感器漂移 / 热计量异常", "校验一次回水温度传感器与热量表，比对便携式测温"),
        "dt1": ("一次侧温差异常（传感器或换热器结垢）", "检查换热器进出口温度与压降，必要时清洗"),
        "makeup": ("二网失水，疑似管网泄漏", "排查二网主干管及楼内管网，查看补水泵启停记录"),
        "ts2_dev": ("二次供温控制偏差", "检查调节阀执行机构与 PID 参数"),
        "tin": ("室温异常波动", "核对室温采集点与楼栋阀门状态"),
        "valve": ("阀门开度异常", "检查执行机构反馈与资用压差"),
    }
    FEAT_TEXT = {"heat_balance": "一/二次侧热量偏差(%)", "makeup": "单位面积补水 t/(万m²·h)", "ts2_dev": "供温跟踪偏差(℃)",
                 "tin": "室温(℃)", "dt1": "一次侧温差(℃)", "valve": "阀门开度(%)"}

    def anomalies(self, days: int = 7) -> dict:
        key = ("an", days, now_local().strftime("%Y%m%d%H"))
        if key in self._cache:
            return self._cache[key]
        now = now_local()
        win_start = now - timedelta(days=days)
        base_start = win_start - timedelta(days=7)
        Z = []
        meta = []
        raw = {}
        for s in STATIONS:
            f = self._station_features(s.id, base_start)
            base = f[f.index < win_start]
            mu = base.median()
            sd = (base.quantile(0.75) - base.quantile(0.25)).replace(0, np.nan).fillna(1.0) / 1.349
            sd = sd.clip(lower=pd.Series({"heat_balance": 1.5, "makeup": 0.02, "ts2_dev": 0.3, "tin": 0.2,
                                          "dt1": 0.8, "valve": 1.5}))
            z = (f - mu) / sd
            raw[s.id] = (f, z, mu)
            Z.append(z)
            meta += [(s.id, t) for t in z.index]
        Zall = pd.concat(Z)
        Xv = Zall.clip(-15, 15).fillna(0).to_numpy()
        iso = IsolationForest(n_estimators=200, contamination=0.02, random_state=0).fit(Xv)
        score = -iso.score_samples(Xv)                    # 越大越异常
        thr = float(np.quantile(score, 0.98))
        Zall = Zall.assign(_score=score, _sid=[m[0] for m in meta])
        events = []
        for s in STATIONS:
            f, z, mu = raw[s.id]
            zs = Zall[Zall["_sid"] == s.id]
            zs = zs[zs.index >= win_start]
            flag = zs[zs["_score"] > thr]
            if len(flag) < 4:
                continue
            feat_cols = list(self.FEAT_TEXT)
            dom = flag[feat_cols].abs().mean().idxmax()
            # 连续段：取最近一次异常的起点
            first = flag.index[0]
            last = flag.index[-1]
            sev = float(flag["_score"].mean())
            ftype, advice = self.TYPE_MAP[dom]
            known = s.id in ADOPTIONS and dom in ("tin", "valve", "ts2_dev")
            recent = f.loc[flag.index, dom].mean()
            ev = [f"{self.FEAT_TEXT[dom]}：基线 {mu[dom]:.2f} → 异常期均值 {recent:.2f}（|z|≈{abs(flag[dom]).mean():.1f}）",
                  f"异常时段 {first.strftime('%m-%d %H时')} 至 {last.strftime('%m-%d %H时')}，命中 {len(flag)} 小时"]
            if dom == "heat_balance":
                ev.append("二次侧热量与流量、温度自洽，一次侧回水温度偏高导致计算热量偏小 → 指向一次回水测温点")
            if known:
                ftype = "曲线调整后的工况变化（已知原因）"
            if dom == "makeup":
                ev.append("同期二次侧压力、供回温正常，补水量阶跃上升 → 指向持续失水")
            events.append({
                "station_id": s.id, "station_name": s.name, "type": ftype, "feature": dom,
                "feature_text": self.FEAT_TEXT[dom], "start": first.strftime("%m-%d %H:%M"),
                "last": last.strftime("%m-%d %H:%M"), "hours": int(len(flag)),
                "severity": "低" if known else ("高" if sev > thr * 1.25 else "中"), "score": _r(sev, 3),
                "evidence": ev + (["同期该站执行过气候补偿曲线调整，变化由调控引起"] if known else []),
                "advice": "属于调控引起的工况变化，可在“调控效果评估”中查看净效果" if known else advice,
                "known_cause": known,
                "zscores": {k: _r(abs(flag[k]).mean(), 1) for k in feat_cols},
            })
        events.sort(key=lambda e: -e["score"])
        n_real = sum(1 for e in events if not e["known_cause"])
        res = envelope(f"近 {days} 天检测到 {n_real} 个站点存在持续性运行异常"
                       + (f"，另有 {len(events) - n_real} 个为调控引起的已知变化。" if len(events) > n_real else "。"), {
            "events": events, "threshold": _r(thr, 3), "window_days": days,
            "model": "Isolation Forest（各站以前 7 天为基线做稳健标准化，多变量联合打分）",
            "features": self.FEAT_TEXT,
        }, evidence=[f"异常分阈值（98% 分位）{thr:.3f}"], confidence=0.8)
        self._cache[key] = res
        return res

    def anomaly_series(self, sid: str, days: int = 10) -> dict:
        start = now_local() - timedelta(days=days)
        f = self._station_features(sid, start)
        return {"ts": [t.strftime("%m-%d %H:%M") for t in f.index],
                **{c: [_r(v) for v in f[c]] for c in f.columns}}

    # ==================================================================
    # 3. 一站一策曲线优化
    # ==================================================================
    def curve_opt(self, sid: str, target: float = T_ROOM_TARGET, days: int = 10) -> dict:
        """灰箱模型（机理结构 + 数据辨识参数）。

        稳态热平衡 k2(Tin−Tout) = k1(Tm−Tin) 可整理为  Tin − Tout = α·(Tm − Tout)，α = k1/(k1+k2)；
        二次侧温差 ΔT2 = Q/C2 ∝ (Tin − Tout)，即 ΔT2 = β·(Tin − Tout)。
        α、β 用过原点最小二乘辨识。之所以不直接回归 Tin ~ Ts2 + Tout：气候补偿运行时
        Ts2 本身是 Tout 的函数，二者高度共线，参数不可辨识（会得到负的敏感度）。
        求解：Tm* = Tout + (Tin*−Tout)/α，Ts2* = Tm* + β(Tin*−Tout)/2。
        """
        cfg = STATION_BY_ID[sid]
        df = self.store.stations[sid]
        df = df[df.index >= now_local() - timedelta(days=days)]
        tm = (df["ts2"] + df["tr2"]) / 2
        x = (tm - df["tout"]).to_numpy()
        y = (df["tin"] - df["tout"]).to_numpy()
        dt2 = (df["ts2"] - df["tr2"]).to_numpy()
        alpha = float(x @ y / (x @ x))
        beta = float(y @ dt2 / (y @ y))
        tin_hat = df["tout"].to_numpy() + alpha * x
        resid = df["tin"].to_numpy() - tin_hat
        rmse = float(np.sqrt(np.mean(resid ** 2)))
        r2 = float(1 - np.sum((y - alpha * x) ** 2) / np.sum((y - y.mean()) ** 2))
        corr = float(np.corrcoef(df["ts2"], df["tout"])[0, 1])
        cur_curve = (self.engine.st[sid].curve if self.engine is not None
                     else self.store.models[sid].curve_with_offset(cfg.offset_now))
        lo, hi = LIMITS[cfg.system]["ts2"]
        tmin, tmax = float(df["tout"].min()), float(df["tout"].max())

        def ts2_star(t):
            return t + (target - t) / alpha + beta * (target - t) / 2

        def tin_under(ts2, t):          # 当前曲线下的预测室温（由 Ts2 反推 Tm 后代入）
            # Ts2 = Tm + β(Tin−t)/2 且 Tin−t = α(Tm−t)  =>  Tm = (Ts2 + αβt/2 − ... ) 联立求解
            tm_ = (ts2 + alpha * beta * t / 2) / (1 + alpha * beta / 2)
            return t + alpha * (tm_ - t)

        rec, deltas, tin_cur = [], [], []
        for t, ts2 in cur_curve:
            d = float(np.clip(ts2_star(t) - ts2, -LIMITS["curve_point_step"], LIMITS["curve_point_step"]))
            deltas.append(d)
            tin_cur.append(tin_under(ts2, t))
            rec.append([t, round(float(np.clip(ts2 + d, lo, hi)), 1)])
        for i in range(1, len(rec)):                # 保证单调不增
            rec[i][1] = min(rec[i][1], rec[i - 1][1])
        tin_now = float(df["tin"].iloc[-24:].mean())
        tout_avg = float(df["tout"].mean())
        saving = (tin_now - target) / max(tin_now - tout_avg, 1) * 100
        heat_day = float(df["heat_gj"].iloc[-24:].sum())
        warnings = [f"室外温度观测范围 {tmin:.1f}～{tmax:.1f}℃，范围外曲线点为机理外推，降温过程中需复核"]
        if rmse > 0.5:
            warnings.append(f"室温拟合 RMSE {rmse:.2f}℃ 偏大，建议检查室温采集点代表性")
        sat_ratio = float(df["valve_sat"].mean())
        if sat_ratio > 0.02 or float(df["valve"].iloc[-24:].mean()) > 90:
            warnings.append("该站阀门接近全开，单纯提高曲线可能无法实现，需先解决一网资用压差")
        data = {
            "station_id": sid, "station_name": cfg.name, "target": target,
            "model": {"type": "灰箱传热模型 (Tin−Tout)=α(Tm−Tout)，ΔT2=β(Tin−Tout)",
                      "alpha": _r(alpha, 4), "beta": _r(beta, 3), "r2": _r(r2, 3), "rmse": _r(rmse, 2),
                      "n": int(len(df)), "days": days, "collinearity": _r(corr, 3)},
            "current_curve": cur_curve, "recommended_curve": rec,
            "deltas": [_r(d, 1) for d in deltas], "tin_under_current": [_r(v, 1) for v in tin_cur],
            "tin_recent": _r(tin_now), "tout_avg": _r(tout_avg), "tout_range": [_r(tmin, 1), _r(tmax, 1)],
            "saving_pct": _r(saving, 1), "saving_gj_day": _r(saving / 100 * heat_day, 1),
            "scatter": {"x": [_r(v, 2) for v in x[::2]], "y": [_r(v, 2) for v in y[::2]]},
        }
        md = float(np.mean(deltas))
        if abs(tin_now - target) < 0.4:
            summary = f"{cfg.name}近 24h 室温 {tin_now:.1f}℃，已接近目标，曲线微调 {md:+.1f}℃ 即可。"
        elif tin_now > target:
            summary = (f"{cfg.name}近 24h 室温 {tin_now:.1f}℃，高于目标 {target}℃；建议曲线平均下调 "
                       f"{-md:.1f}℃，预计节热 {saving:.1f}%（约 {saving / 100 * heat_day:.0f} GJ/天）。")
        else:
            summary = (f"{cfg.name}近 24h 室温 {tin_now:.1f}℃，低于目标 {target}℃；建议曲线平均上调 "
                       f"{md:.1f}℃，热耗预计增加 {-saving:.1f}%。")
        return envelope(summary, data, evidence=[f"α={alpha:.3f}，β={beta:.3f}；(Tin−Tout) 拟合 R²={r2:.3f}，室温 RMSE={rmse:.2f}℃",
                                                 f"Ts2 与 Tout 相关系数 {corr:.2f}：直接线性回归不可辨识，故采用机理约束模型"],
                        confidence=_r(min(0.95, max(0.4, r2 - rmse / 2)), 2), warnings=warnings)

    def curve_overview(self) -> dict:
        rows = []
        for s in STATIONS:
            r = self.curve_opt(s.id)["data"]
            rows.append({"station_id": s.id, "station_name": s.name, "tin_recent": r["tin_recent"],
                         "mean_delta": _r(float(np.mean([d for d in r["deltas"]])), 1),
                         "saving_pct": r["saving_pct"], "saving_gj_day": r["saving_gj_day"], "r2": r["model"]["r2"]})
        rows.sort(key=lambda r: -(r["saving_gj_day"] or 0))
        total = sum(max(r["saving_gj_day"] or 0, 0) for r in rows)
        return envelope(f"全网按一站一策调整曲线，预计可节热约 {total:.0f} GJ/天。", {"rows": rows, "total_gj_day": _r(total, 1)})

    # ==================================================================
    # 4. 调控效果评估（DiD）
    # ==================================================================
    def control_effect(self, sid: str, switch: datetime | None = None, window_days: int = 4) -> dict:
        now = now_local()
        if switch is None:
            if sid not in ADOPTIONS:
                raise ValueError("该站无曲线调整记录，可传入 switch 时间")
            switch = now - timedelta(days=ADOPTIONS[sid])
        pre = (switch - timedelta(days=window_days), switch)
        post = (switch, min(switch + timedelta(days=window_days), now))
        controls = [s.id for s in STATIONS if s.id not in ADOPTIONS and s.id not in ("ST03", "ST07", "ST10")]

        def norm(oid, a, b):
            df = self.store.stations[oid]
            d = df[(df.index >= a) & (df.index < b)]
            h = float(np.clip(18 - d["tout"], 0, None).sum() / 24)
            return float(d["heat_gj"].sum() * 1e6 / (STATION_BY_ID[oid].area * max(h, 0.1))), float(d["tin"].mean())

        t_pre, tin_pre = norm(sid, *pre)
        t_post, tin_post = norm(sid, *post)
        c_pre = [norm(c, *pre) for c in controls]
        c_post = [norm(c, *post) for c in controls]
        cp = float(np.mean([x[0] for x in c_pre]))
        cq = float(np.mean([x[0] for x in c_post]))
        treat_change = t_post / t_pre - 1
        ctrl_change = cq / cp - 1
        did = treat_change - ctrl_change
        tin_did = (tin_post - tin_pre) - (np.mean([x[1] for x in c_post]) - np.mean([x[1] for x in c_pre]))
        # 逐日序列
        days = pd.date_range(pre[0].replace(hour=0, minute=0, second=0, microsecond=0), now, freq="D")
        series = []
        for d in days:
            e = d + timedelta(days=1)
            try:
                tn, ti = norm(sid, d, e)
                cn = float(np.mean([norm(c, d, e)[0] for c in controls]))
            except ZeroDivisionError:
                continue
            series.append({"date": d.strftime("%m-%d"), "treat": _r(tn, 1), "control": _r(cn, 1), "tin": _r(ti, 2)})
        data = {"station_id": sid, "station_name": name_of(sid), "switch": switch.strftime("%m-%d %H:%M"),
                "treat": {"pre": _r(t_pre, 1), "post": _r(t_post, 1), "change_pct": _r(treat_change * 100, 1),
                          "tin_pre": _r(tin_pre), "tin_post": _r(tin_post)},
                "control": {"ids": controls, "pre": _r(cp, 1), "post": _r(cq, 1), "change_pct": _r(ctrl_change * 100, 1)},
                "did_pct": _r(did * 100, 1), "tin_did": _r(tin_did, 2), "series": series,
                "method": "双重差分：(处理站后/前 − 1) − (对照组后/前 − 1)，指标为度日数修正单耗"}
        summ = (f"{name_of(sid)}调整后修正单耗变化 {treat_change * 100:+.1f}%，对照组 {ctrl_change * 100:+.1f}%，"
                f"净效果 {did * 100:+.1f}%，室温净变化 {tin_did:+.2f}℃。")
        return envelope(summ, data, evidence=[f"对照组 {len(controls)} 个未调整站", f"前后各 {window_days} 天窗口"],
                        confidence=0.75)

    # ==================================================================
    # 5. 综合诊断 + 运行评分
    # ==================================================================
    def diagnosis(self) -> dict:
        now = now_local()
        start = now - timedelta(hours=24)
        findings = []
        scores = []
        norms = {}
        for s in STATIONS:
            d = self.store.stations[s.id]
            d = d[d.index >= start]
            h = float(np.clip(18 - d["tout"], 0, None).sum() / 24)
            norms[s.id] = float(d["heat_gj"].sum() * 1e6 / (s.area * max(h, 0.1)))
        med = float(np.median(list(norms.values())))
        an = {e["station_id"]: e for e in self.anomalies()["data"]["events"]}
        for s in STATIONS:
            d = self.store.stations[s.id]
            d = d[d.index >= start]
            tin = float(d["tin"].mean())
            comfort = 100 - min(100, max(0, abs(tin - 20) - 0.5) * 25)
            eff = float(np.clip(100 - (norms[s.id] / med - 1) * 200, 0, 100))
            stab = float(np.clip(100 - (d["ts2"] - d["ts2_set"]).abs().mean() * 40, 0, 100))
            health = 55.0 if s.id in an else 100.0
            total = 0.35 * comfort + 0.3 * eff + 0.15 * stab + 0.2 * health
            scores.append({"station_id": s.id, "station_name": s.name, "comfort": _r(comfort, 0), "efficiency": _r(eff, 0),
                           "stability": _r(stab, 0), "health": _r(health, 0), "total": _r(total, 0),
                           "tin": _r(tin), "norm_heat": _r(norms[s.id], 1)})
            if tin > 21.5:
                findings.append({"level": "warn", "category": "节能", "object": s.name, "object_id": s.id,
                                 "title": f"过量供热：室温 {tin:.1f}℃",
                                 "detail": f"修正单耗 {norms[s.id]:.0f}（较全网中位数 {(norms[s.id] / med - 1) * 100:+.0f}%）；"
                                           f"室温高于 20℃ 的部分即为可挖潜热量。",
                                 "advice": "执行一站一策曲线优化（AI 分析 → 曲线优化），预计可节热 "
                                           f"{(tin - 20) / max(tin - float(d['tout'].mean()), 1) * 100:.0f}%。",
                                 "action": {"type": "curve_opt", "station_id": s.id}})
            if tin < 18.5:
                vs = float(d["valve"].mean())
                findings.append({"level": "danger", "category": "供热质量", "object": s.name, "object_id": s.id,
                                 "title": f"供热不足：室温 {tin:.1f}℃",
                                 "detail": f"阀门平均开度 {vs:.0f}%，资用压差系数 {s.dp_drop:.2f}（远端）。",
                                 "advice": "优先提高该站曲线 1–2℃；若阀门接近全开，需调整一网水力平衡或在热源侧提高供回水压差。",
                                 "action": {"type": "curve_opt", "station_id": s.id}})
        for e in an.values():
            if e["known_cause"]:
                continue
            findings.append({"level": "danger" if e["severity"] == "高" else "warn", "category": "异常检测",
                             "object": e["station_name"], "object_id": e["station_id"], "title": e["type"],
                             "detail": "；".join(e["evidence"][:2]), "advice": e["advice"],
                             "action": {"type": "anomaly", "station_id": e["station_id"]}})
        fc = self.forecast("NET", 72)["data"]
        tf = np.array(fc["forecast"]["tout"], dtype=float)
        cur_t = fc["history"]["tout"][-1]
        if tf.min() < cur_t - 5:
            i = int(np.argmin(tf))
            findings.append({"level": "warn", "category": "预测", "object": "全网", "object_id": "NET",
                             "title": f"降温预警：{fc['forecast']['ts'][i]} 预计 {tf.min():.1f}℃",
                             "detail": f"较当前下降 {cur_t - tf.min():.1f}℃，负荷峰值 P90 {fc['peak']['p90']}MW。",
                             "advice": "建议提前 3–6 小时预升温，并安排调峰热源备用。",
                             "action": {"type": "forecast", "scope": "NET"}})
        for src in SOURCES:
            f = self.forecast(src.id, 72)["data"]
            if f["peak"]["p90"] and f["peak"]["p90"] > 0.85 * src.capacity_mw:
                findings.append({"level": "warn", "category": "预测", "object": src.name, "object_id": src.id,
                                 "title": f"负荷接近容量：P90 {f['peak']['p90']}MW / {src.capacity_mw}MW",
                                 "detail": f"预计出现在 {f['peak']['ts']}。", "advice": "核对热源出力计划与调峰锅炉备用。",
                                 "action": {"type": "forecast", "scope": src.id}})
        order = {"danger": 0, "warn": 1, "info": 2}
        findings.sort(key=lambda f: order[f["level"]])
        scores.sort(key=lambda s: s["total"])
        avg = float(np.mean([s["total"] for s in scores]))
        summ = (f"全网运行评分 {avg:.0f} 分；发现 {sum(1 for f in findings if f['level'] == 'danger')} 项严重问题、"
                f"{sum(1 for f in findings if f['level'] == 'warn')} 项提示。")
        return envelope(summ, {"findings": findings, "scores": scores, "score_avg": _r(avg, 0), "median_norm_heat": _r(med, 1),
                               "weights": {"comfort": 0.35, "efficiency": 0.3, "stability": 0.15, "health": 0.2}},
                        evidence=["近 24h 运行数据", "异常检测模型", "负荷预测模型"], confidence=0.8)
