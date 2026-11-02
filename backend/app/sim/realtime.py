"""实时仿真引擎：代替 SCADA 采集层，每个 tick 推进一次全网状态。

- 阀门执行机构有行程速度（1%/s），温度有一阶惯性，室温惯性更大，便于观察调控效果；
- 控制模式：curve 气候补偿 / fixed 定供温 / manual 手动阀位；
- 整点把上一小时的积分量写入 HistoryStore，统计和 AI 模块即可用到最新数据。
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timedelta

import numpy as np

from ..config import (ANOMALIES, CP, GAS_LHV_MJ, LIMITS, SOURCE_BY_ID, SOURCES, STATIONS,
                      T_BALANCE, T_DESIGN_OUT)
from .history import ADOPTIONS, HP_CAP, HistoryStore, elec_price, load_ratio, now_local
from .physics import StationModel, interp_curve
from .weather import outdoor_temp

TAU_FAST = 40.0     # 水温/流量惯性 s
TAU_ROOM = 900.0    # 室温惯性 s
VALVE_SPEED = 1.0   # %/s

LEVEL_TEXT = {1: "一级", 2: "二级", 3: "三级"}


def _lag(cur, target, dt, tau):
    return cur + (target - cur) * (1 - math.exp(-dt / tau))


class StationState:
    def __init__(self, model: StationModel, last_row):
        cfg = model.cfg
        self.model = model
        self.mode = "curve"
        offset = cfg.offset_now
        self.curve = model.curve_with_offset(offset)
        self.ts2_fixed = round(float(last_row["ts2_set"]), 1)
        self.valve_set = round(float(last_row["valve"]), 1)
        self.valve_pos = float(last_row["valve"])
        self.freq = float(cfg.pump_freq)
        self.d = {k: float(last_row[k]) for k in ("ts1", "tr1", "g1", "ts2", "tr2", "g2", "heat_mw", "tin")}
        self.d["ts2_set"] = float(last_row["ts2_set"])
        self.online = True
        self.acc = {"heat_gj": 0.0, "elec_kwh": 0.0, "water_t": 0.0, "n": 0}
        self.hour_sum: dict[str, float] = {}
        self.extra: dict = {}


class Engine:
    def __init__(self, store: HistoryStore):
        self.store = store
        self.started = time.time()
        self.t = time.time()
        self.hour = now_local().replace(minute=0, second=0, microsecond=0)
        self.rng = np.random.default_rng(7)
        self.st: dict[str, StationState] = {}
        for cfg in STATIONS:
            last = store.stations[cfg.id].iloc[-1]
            self.st[cfg.id] = StationState(store.models[cfg.id], last)
        self.ts1_noise = {s.id: 0.0 for s in SOURCES}
        last_src = store.sources["HS03"].iloc[-1]
        self.tank_soc = float(last_src["tank_soc"]) if last_src["tank_soc"] > 0 else 40.0
        self.src_hour_sum: dict[str, dict] = {s.id: {} for s in SOURCES}
        self.src_snap: dict[str, dict] = {}
        self.alarms: dict[str, dict] = {}
        self.alarm_log: list[dict] = []
        self.snapshot: dict = {}
        # 外部报警来源（如熔断）：返回 [(级别, 代码, 对象 ID, 对象名, 内容)]
        self.alarm_hooks: list = []
        self.tick(0.0)

    # ------------------------------------------------------------------
    def _ts2_target(self, s: StationState, tout: float):
        if s.mode == "curve":
            return float(interp_curve(tout, s.curve))
        if s.mode == "fixed":
            return s.ts2_fixed
        return None

    def predict(self, station_id: str, override: dict) -> dict:
        """在当前工况下，预测某站参数变更后的稳态结果（安全校验中的仿真验证）。"""
        s = self.st[station_id]
        m = s.model
        tout = self.snapshot["tout"]
        ts1 = s.d["ts1"]
        mode = override.get("mode", s.mode)
        curve = override.get("curve", s.curve)
        ts2_fixed = override.get("ts2_fixed", s.ts2_fixed)
        valve = override.get("valve_set", s.valve_set)
        freq = override.get("freq", s.freq)
        dpr = float(m.dp_ratio(load_ratio(tout)))
        sat = False
        if mode == "curve":
            sp = float(interp_curve(tout, curve))
        elif mode == "fixed":
            sp = ts2_fixed
        else:
            sp = None
        if sp is not None:
            v, sat = m.valve_for_ts2(np.array([ts1]), tout, sp, dpr, freq)
            valve = float(v[0]) * 100
            sat = bool(sat[0])
        r = m.solve(ts1, tout, valve / 100, dpr, freq)
        return {"ts2_set": sp, "valve": round(valve, 1), "saturated": sat,
                **{k: round(float(v), 2) for k, v in r.items()}}

    # ------------------------------------------------------------------
    def tick(self, dt: float):
        self.t = time.time()
        now = now_local()
        if now.replace(minute=0, second=0, microsecond=0) > self.hour:
            self._roll_hour()
            self.hour = now.replace(minute=0, second=0, microsecond=0)
        tout = float(outdoor_temp(self.t))
        lr = float(load_ratio(tout))
        price = float(elec_price(np.array([now.hour]))[0])
        stations = {}
        by_src: dict[str, list] = {s.id: [] for s in SOURCES}
        for sid, s in self.st.items():
            cfg, m = s.model.cfg, s.model
            src = SOURCE_BY_ID[cfg.source_id]
            self.ts1_noise[src.id] = 0.98 * self.ts1_noise[src.id] + self.rng.normal(0, 0.05)
            ts1 = float(interp_curve(tout, src.curve)) + self.ts1_noise[src.id] - 0.6 * cfg.dp_drop
            dpr = float(m.dp_ratio(lr))
            sp = self._ts2_target(s, tout)
            if sp is not None:
                v, sat = m.valve_for_ts2(np.array([ts1]), tout, sp, dpr, s.freq)
                vt = float(v[0]) * 100
                s.valve_set = round(vt, 1)
            else:
                vt = s.valve_set
            step = VALVE_SPEED * max(dt, 0.0)
            s.valve_pos += float(np.clip(vt - s.valve_pos, -step, step)) if dt > 0 else (vt - s.valve_pos)
            r = m.solve(ts1, tout, s.valve_pos / 100, dpr, s.freq)
            tgt = {k: float(v) for k, v in r.items()}
            tgt["ts1"] = ts1
            for k in ("ts1", "tr1", "g1", "ts2", "tr2", "g2", "heat_mw"):
                s.d[k] = _lag(s.d[k], tgt[k], dt, TAU_FAST) if dt > 0 else s.d[k]
            s.d["tin"] = _lag(s.d["tin"], tgt["tin"], dt, TAU_ROOM) if dt > 0 else s.d["tin"]
            s.d["ts2_set"] = sp if sp is not None else s.d["ts2"]

            n = lambda sd: float(self.rng.normal(0, sd))
            tr1_meas = s.d["tr1"] + n(0.08)
            makeup = max(cfg.makeup_base * (1 + n(0.15)), 0)
            an = ANOMALIES.get(sid, {})
            if an.get("type") == "tr1_sensor_drift":
                tr1_meas += an["magnitude"]
            if an.get("type") == "makeup_leak":
                makeup += an["magnitude"] * (1 + n(0.08))
            s.online = not (an.get("type") == "comm_intermittent" and now.minute % 20 < 2)
            p1s = 0.9 - 0.30 * cfg.dp_drop * lr ** 2 + n(0.002)
            p1r = 0.32 + 0.12 * cfg.dp_drop * lr ** 2 + n(0.002)
            p2r = 0.28 + n(0.002)
            p2s = p2r + 0.13 * (s.freq / 45) ** 2 + n(0.002)
            pump_kw = cfg.pump_kw * (s.freq / 50) ** 3 * 0.85 + n(0.3)
            q1 = s.d["g1"] / 3.6 * CP * (s.d["ts1"] - tr1_meas) / 1000
            q2 = s.d["g2"] / 3.6 * CP * (s.d["ts2"] - s.d["tr2"]) / 1000
            rec = {
                "id": sid, "name": cfg.name, "source_id": cfg.source_id, "online": s.online,
                "mode": s.mode, "tout": tout, "ts1": s.d["ts1"], "tr1": tr1_meas, "p1s": p1s, "p1r": p1r,
                "g1": s.d["g1"], "ts2": s.d["ts2"] + n(0.05), "tr2": s.d["tr2"] + n(0.05), "p2s": p2s,
                "p2r": p2r, "g2": s.d["g2"], "ts2_set": s.d["ts2_set"], "valve": s.valve_pos,
                "valve_set": s.valve_set, "tin": s.d["tin"], "heat_mw": s.d["heat_mw"], "q1_mw": q1,
                "q2_mw": q2, "pump_freq": s.freq, "pump_kw": pump_kw, "makeup_tph": makeup,
                "tank_level": 2.2 - min(makeup, 2) * 0.2 + n(0.01),
                "valve_sat": s.valve_pos > 99 and s.d["ts2"] < s.d["ts2_set"] - 1.5,
                "autonomy": cfg.autonomy, "system": cfg.system, "area": cfg.area,
            }
            # 小时积分
            if dt > 0:
                h = s.hour_sum
                for k in ("tout", "ts1", "tr1", "g1", "ts2", "tr2", "g2", "ts2_set", "valve", "tin",
                          "p1s", "p1r", "p2s", "p2r", "heat_mw", "q1_mw", "q2_mw", "pump_freq"):
                    h[k] = h.get(k, 0.0) + rec[k] * dt
                h["elec_kwh"] = h.get("elec_kwh", 0.0) + (pump_kw + 3) * dt / 3600
                h["water_t"] = h.get("water_t", 0.0) + makeup * dt / 3600
                h["_t"] = h.get("_t", 0.0) + dt
            if not s.online:
                prev = self.snapshot.get("stations", {}).get(sid)
                if prev:
                    rec = {**prev, "online": False}
            stations[sid] = rec
            by_src[cfg.source_id].append(rec)

        sources = {src.id: self._source_snapshot(src, by_src[src.id], tout, dt, now, price) for src in SOURCES}
        net_heat = sum(s["heat_mw"] for s in sources.values())
        self.snapshot = {
            "ts": now.isoformat(timespec="seconds"), "tout": tout, "stations": stations, "sources": sources,
            "network": {
                "heat_mw": net_heat,
                "capacity_mw": sum(s.capacity_mw for s in SOURCES),
                "flow_tph": sum(s["g"] for s in sources.values()),
                "tin_avg": float(np.mean([s["tin"] for s in stations.values()])),
                "online": sum(1 for s in stations.values() if s["online"]),
                "total": len(stations),
                "area": sum(c.area for c in STATIONS),
                "elec_price": price,
            },
        }
        self._eval_alarms()
        self.snapshot["alarms"] = self.active_alarms()
        self.snapshot["today"] = self.today_totals()

    def _source_snapshot(self, src, recs, tout, dt, now, price):
        g = sum(r["g1"] for r in recs)
        tr1 = sum(r["g1"] * (r["tr1"] if r["id"] not in ANOMALIES else self.st[r["id"]].d["tr1"]) for r in recs) / max(g, 1e-6) - 0.3
        ts1 = float(interp_curve(tout, src.curve)) + self.ts1_noise[src.id]
        heat = sum(r["heat_mw"] for r in recs) * 1.03
        g_design = sum(self.store.stations[r["id"]]["g1"].max() for r in recs)
        ratio = g / max(g_design, 1e-6)
        pump_kw = src.pump_kw * max(ratio, 0.3) ** 3 * 0.9
        units = []
        gas = steam = prod_kw = 0.0
        extra = {}
        pumps = [u for u in src.units if u["type"] == "pump"]
        n_pump_run = max(1, min(len(pumps) - 1, math.ceil(ratio * (len(pumps) - 1) + 0.2)))
        pump_freq = 30 + 20 * min(ratio * (len(pumps) - 1) / n_pump_run, 1.0)
        if src.kind == "chp":
            hxs = [u for u in src.units if u["type"] == "hx"]
            n_run = max(1, math.ceil(heat / (hxs[0]["cap_mw"] * 0.85)))
            steam = heat * 3.6 / 2.30 * 1.01
            for i, u in enumerate(hxs):
                on = i < n_run
                units.append({**u, "running": on, "load": heat / n_run / u["cap_mw"] * 100 if on else 0,
                              "steam_tph": steam / n_run if on else 0, "outlet": ts1 + 0.4 if on else tr1})
            extra = {"steam_tph": steam, "steam_p": 0.62, "steam_t": 248.0, "condensate_t": tr1 + 6}
        elif src.kind == "gas":
            bs = [u for u in src.units if u["type"] == "boiler"]
            n_run = max(1, math.ceil(heat / (bs[0]["cap_mw"] * 0.92)))
            part = heat / (n_run * bs[0]["cap_mw"])
            eta = 0.93 - 0.06 * abs(part - 0.75)
            gas = heat * 3600 / (eta * GAS_LHV_MJ)
            for i, u in enumerate(bs):
                on = i < n_run
                units.append({**u, "running": on, "load": part * 100 if on else 0, "outlet": ts1 + 0.6 if on else tr1,
                              "gas_nm3h": gas / n_run if on else 0, "flue_t": 58 + 30 * part if on else 25,
                              "eff": eta * 100 if on else 0})
            extra = {"gas_nm3h": gas, "eff": eta * 100}
        else:
            hp = min(heat, HP_CAP)
            rest = heat - hp
            cap = 70.0
            soc = self.tank_soc / 100 * cap
            if now.hour >= 23 or now.hour < 7:
                eb = min(10.0, rest + (cap - soc))
                charge = eb - rest
                dis = 0.0
            else:
                dis = min(rest, soc)
                eb = rest - dis
                charge = -dis
            self.tank_soc = float(np.clip((soc + charge * dt / 3600) / cap * 100, 0, 100))
            cop = 4.3 - 0.03 * (ts1 - 60)
            prod_kw = hp * 1000 / cop + eb * 1000 / 0.99
            hps = [u for u in src.units if u["type"] == "heatpump"]
            for u in hps:
                ld = hp / len(hps)
                units.append({**u, "running": ld > 0.05, "load": ld / u["cap_mw"] * 100, "cop": cop,
                              "outlet": ts1 - 1})
            units.append({"id": "EB1", "name": "电极锅炉", "type": "eboiler", "running": eb > 0.05,
                          "load": eb / 10 * 100, "power_kw": eb * 1000 / 0.99})
            units.append({"id": "TK1", "name": "蓄热罐", "type": "tank", "running": True, "soc": self.tank_soc,
                          "mode": "蓄热" if charge > 0.05 else ("放热" if dis > 0.05 else "静置"),
                          "top_t": 92 - (100 - self.tank_soc) * 0.1, "bottom_t": 50 + self.tank_soc * 0.05})
            extra = {"hp_mw": hp, "eb_mw": eb, "tank_dis_mw": dis, "tank_soc": self.tank_soc, "cop": cop,
                     "power_kw": prod_kw}
        for i, u in enumerate(pumps):
            on = i < n_pump_run
            units.append({**u, "running": on, "freq": pump_freq if on else 0,
                          "power_kw": pump_kw / n_pump_run if on else 0})
        makeup = 2.0 * (1 + float(self.rng.normal(0, 0.1)))
        rec = {
            "id": src.id, "name": src.name, "kind": src.kind, "tout": tout, "ts1": ts1, "tr1": tr1, "g": g,
            "p_s": src.p_supply - 0.05 * float(load_ratio(tout)) + float(self.rng.normal(0, 0.002)),
            "p_r": src.p_return + float(self.rng.normal(0, 0.002)), "heat_mw": heat,
            "capacity_mw": src.capacity_mw, "load_pct": heat / src.capacity_mw * 100,
            "pump_kw": pump_kw, "makeup_tph": makeup, "units": units, **extra,
        }
        if dt > 0:
            h = self.src_hour_sum[src.id]
            for k in ("tout", "ts1", "tr1", "g", "p_s", "p_r", "heat_mw"):
                h[k] = h.get(k, 0.0) + rec[k] * dt
            h["elec_kwh"] = h.get("elec_kwh", 0.0) + (pump_kw + 40 + prod_kw) * dt / 3600
            h["water_t"] = h.get("water_t", 0.0) + makeup * dt / 3600
            h["gas_nm3"] = h.get("gas_nm3", 0.0) + gas * dt / 3600
            h["steam_t"] = h.get("steam_t", 0.0) + steam * dt / 3600
            h["cost_yuan"] = h.get("cost_yuan", 0.0) + (
                (gas * 3.2 if src.kind == "gas" else 0) + (heat * 3.6 * 42 if src.kind == "chp" else 0)
                + (pump_kw + 40 + prod_kw) * price + makeup * 6.5) * dt / 3600
            h["_t"] = h.get("_t", 0.0) + dt
        return rec

    def _roll_hour(self):
        """把上一个小时的积分写入历史库。"""
        ts = self.hour
        for sid, s in self.st.items():
            h = s.hour_sum
            T = h.get("_t", 0.0)
            if T < 60:
                s.hour_sum = {}
                continue
            row = {k: v / T for k, v in h.items() if k not in ("_t", "elec_kwh", "water_t")}
            scale = 3600 / T
            row.update({"elec_kwh": h["elec_kwh"] * scale, "water_t": h["water_t"] * scale,
                        "heat_gj": row["heat_mw"] * 3.6, "tr1_true": s.d["tr1"], "valve_sat": False,
                        "curve_offset": s.model.cfg.offset_now, "season": "this"})
            self.store.append_hour(sid, ts, row)
            s.hour_sum = {}
        for src in SOURCES:
            h = self.src_hour_sum[src.id]
            T = h.get("_t", 0.0)
            if T < 60:
                self.src_hour_sum[src.id] = {}
                continue
            scale = 3600 / T
            row = {k: v / T for k, v in h.items() if k in ("tout", "ts1", "tr1", "g", "p_s", "p_r", "heat_mw")}
            row.update({k: h[k] * scale for k in ("elec_kwh", "water_t", "gas_nm3", "steam_t", "cost_yuan")})
            row.update({"heat_gj": row["heat_mw"] * 3.6, "season": "this", "tank_soc": self.tank_soc,
                        "hp_mw": 0.0, "eb_mw": 0.0, "tank_dis_mw": 0.0})
            self.store.append_hour(src.id, ts, row)
            self.src_hour_sum[src.id] = {}

    # ------------------------------------------------------------------
    def today_totals(self) -> dict:
        today = self.hour.replace(hour=0)
        net = {"heat_gj": 0.0, "elec_kwh": 0.0, "water_t": 0.0, "gas_nm3": 0.0, "steam_t": 0.0, "cost_yuan": 0.0}
        per = {}
        for oid, df in list(self.store.stations.items()) + list(self.store.sources.items()):
            part = df.loc[df.index >= today]
            d = {k: float(part[k].sum()) for k in net if k in part.columns}
            hs = (self.st[oid].hour_sum if oid in self.st else self.src_hour_sum[oid])
            T = hs.get("_t", 0.0)
            if T > 0:
                d["heat_gj"] = d.get("heat_gj", 0) + hs.get("heat_mw", 0) / T * 3.6 * T / 3600
                for k in ("elec_kwh", "water_t", "gas_nm3", "steam_t", "cost_yuan"):
                    if k in hs:
                        d[k] = d.get(k, 0) + hs[k]
            per[oid] = d
        for oid, d in per.items():
            if oid.startswith("HS"):
                for k in net:
                    net[k] += d.get(k, 0.0)
            else:
                net["elec_kwh"] += d.get("elec_kwh", 0.0)
                net["water_t"] += d.get("water_t", 0.0)
        return {"network": net, "objects": per}

    # ------------------------------------------------------------------
    def _raise(self, key, obj_id, obj_name, level, code, msg, value=None):
        now = self.snapshot["ts"]
        a = self.alarms.get(key)
        if a is None:
            a = {"id": key, "object_id": obj_id, "object_name": obj_name, "level": level, "code": code,
                 "message": msg, "value": value, "first_ts": now, "last_ts": now, "acked": False, "active": True}
            self.alarms[key] = a
            self.alarm_log.insert(0, dict(a))
            del self.alarm_log[300:]
        a.update({"last_ts": now, "value": value, "message": msg, "level": level})

    def _eval_alarms(self):
        seen = set()
        lim = LIMITS
        for sid, r in self.snapshot["stations"].items():
            cfg = self.st[sid].model.cfg
            checks = []
            if not r["online"]:
                checks.append((1, "COMM", "通讯中断", None))
            else:
                hi = lim[cfg.system]["ts2"][1]
                if r["ts2"] > hi - 3:
                    checks.append((2, "TS2_HIGH", f"二次供水温度偏高 {r['ts2']:.1f}℃", r["ts2"]))
                if r["tin"] < 16:
                    checks.append((1, "TIN_LOW", f"典型室温过低 {r['tin']:.1f}℃", r["tin"]))
                elif r["tin"] < 18.5:
                    checks.append((2, "TIN_LOW", f"典型室温偏低 {r['tin']:.1f}℃", r["tin"]))
                if r["tin"] > 23.5:
                    checks.append((3, "TIN_HIGH", f"室温偏高 {r['tin']:.1f}℃，存在过量供热", r["tin"]))
                if r["makeup_tph"] > max(3 * cfg.makeup_base, 0.5):
                    checks.append((1, "MAKEUP", f"补水量异常 {r['makeup_tph']:.2f} t/h，疑似二网泄漏", r["makeup_tph"]))
                if r["valve_sat"]:
                    checks.append((2, "UNDERSUPPLY", "调节阀全开仍达不到供温设定，疑似资用压差不足", r["valve"]))
                if r["q2_mw"] > 0.3 and abs(r["q1_mw"] - r["q2_mw"]) / r["q2_mw"] > 0.12:
                    checks.append((3, "HEAT_BAL", f"一/二次侧热量偏差 {abs(r['q1_mw'] - r['q2_mw']) / r['q2_mw'] * 100:.0f}%，疑似计量或测温异常", None))
                if r["p2r"] < 0.2:
                    checks.append((2, "P2R_LOW", "二次回水压力低", r["p2r"]))
            for level, code, msg, val in checks:
                key = f"{sid}-{code}"
                seen.add(key)
                self._raise(key, sid, r["name"], level, code, msg, val)
        for hid, r in self.snapshot["sources"].items():
            if r["load_pct"] > 90:
                key = f"{hid}-OVERLOAD"
                seen.add(key)
                self._raise(key, hid, r["name"], 2, "OVERLOAD", f"热源负荷率 {r['load_pct']:.0f}%，接近满负荷", r["load_pct"])
            if r["kind"] == "heatpump" and r.get("tank_soc", 100) < 8:
                key = f"{hid}-TANK_LOW"
                seen.add(key)
                self._raise(key, hid, r["name"], 3, "TANK_LOW", "蓄热罐蓄热量低", r["tank_soc"])
        for hook in self.alarm_hooks:
            for level, code, oid, name, msg in hook():
                key = f"{oid}-{code}"
                seen.add(key)
                self._raise(key, oid, name, level, code, msg, None)
        for key in list(self.alarms):
            if key not in seen:
                a = self.alarms.pop(key)
                for log in self.alarm_log:
                    if log["id"] == key and log.get("active"):
                        log.update({"active": False, "cleared_ts": self.snapshot["ts"]})
                        break

    def active_alarms(self):
        return sorted(self.alarms.values(), key=lambda a: (a["level"], a["first_ts"]))

    def ack(self, alarm_id: str, user: str):
        a = self.alarms.get(alarm_id)
        if not a:
            return False
        a["acked"] = True
        a["acked_by"] = user
        return True
