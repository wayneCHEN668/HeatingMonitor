"""运维：楼栋/住户/维修人员主数据、工单、派单推荐、巡检计划、交接班（设计文档第七节 ops_*）。

主数据说明：真实系统中楼栋、住户来自营收/客服系统，维修人员来自人事与排班系统，工单来自工单系统。
这里用固定随机种子生成**演示数据**（姓名、电话、地址均为虚构），接口按“替换数据源即可接入真实系统”设计。
个人信息默认脱敏，完整信息只对有 customer:pii 权限的人工会话返回并记审计。
"""
from __future__ import annotations

import json
import math
import os
import random
import re
import threading
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from ..config import SOURCE_BY_ID, SOURCES, STATION_BY_ID, STATIONS
from ..sim.history import now_local
from .kb import injection_flags

KM_PER_UNIT = 0.02            # 拓扑坐标 1 单位 ≈ 20 m
TRAVEL_KMH = 30.0
VISIT_MIN = 25

WO_TYPES = ["不热", "过热", "漏水", "设备故障", "测量异常", "巡检", "收费", "报停与开栓", "其他"]
PRIORITY_SLA_H = {"紧急": 4, "高": 8, "普通": 24, "低": 72}      # 到场时限（小时）
WO_SKILLS = {
    "不热": {"户内维修", "阀门"}, "过热": {"阀门", "户内维修"}, "漏水": {"管道焊接", "查漏"},
    "设备故障": {"水泵", "电气", "阀门"}, "测量异常": {"仪表"}, "巡检": {"水泵", "阀门", "仪表"},
    "收费": set(), "报停与开栓": {"户内维修"}, "其他": set(),
}
STAFF = [
    ("W01", "刘班长", "维修一班", {"管道焊接", "查漏", "阀门"}, "ST07"),
    ("W02", "孙师傅", "维修一班", {"户内维修", "阀门", "查漏"}, "ST09"),
    ("W03", "周师傅", "维修二班", {"户内维修", "阀门"}, "ST02"),
    ("W04", "吴师傅", "维修二班", {"管道焊接", "查漏"}, "ST04"),
    ("W05", "王师傅", "仪表班", {"仪表", "电气"}, "ST03"),
    ("W06", "赵师傅", "设备班", {"水泵", "电气", "阀门"}, "ST05"),
    ("W07", "郑师傅", "设备班", {"水泵", "阀门"}, "ST12"),
    ("W08", "钱师傅", "维修三班", {"户内维修", "阀门", "查漏"}, "ST11"),
]
_SURNAMES = "王李张刘陈杨黄赵吴周徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾"
_GIVEN = "伟芳娜敏静丽强磊军洋勇艳杰娟涛明超秀霞平刚桂英华建国玉兰志红文斌辉鹏飞"


class OpsError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def mask_name(n: str) -> str:
    return n[0] + "*" * (len(n) - 1) if n else n


def mask_phone(p: str) -> str:
    return p[:3] + "****" + p[-4:] if len(p) >= 8 else "****"


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _dist_km(a: tuple, b: tuple) -> float:
    return math.dist(a, b) * KM_PER_UNIT


# ================================================================ 主数据
class MasterData:
    """楼栋与住户（演示数据，固定种子生成）。"""

    def __init__(self, seed: int = 2026):
        self.buildings: dict[str, dict] = {}
        self.customers: dict[str, dict] = {}
        self.by_phone: dict[str, str] = {}
        for st in STATIONS:
            rng = random.Random(f"{seed}-{st.id}")
            community = st.name.removesuffix("站")
            n_b = max(3, round(st.area / 25000))
            for k in range(1, n_b + 1):
                bid = f"{st.id}-B{k:02d}"
                floors, units = rng.choice([6, 11, 18]), rng.choice([2, 3])
                ang = 2 * math.pi * k / n_b
                b = {"building_id": bid, "station_id": st.id, "name": f"{community}{k}号楼", "floors": floors,
                     "units": units, "households": floors * units * 2,
                     "xy": (st.xy[0] + 18 * math.cos(ang), st.xy[1] + 18 * math.sin(ang)),
                     "temp_offset": round(rng.gauss(0, 0.6), 2)}
                self.buildings[bid] = b
                for u in range(1, units + 1):
                    for f in range(1, floors + 1):
                        for door in (1, 2):
                            cid = f"C{st.id[2:]}{k:02d}{u}{f:02d}{door}"
                            phone = "1" + rng.choice("3589") + "".join(rng.choice("0123456789") for _ in range(9))
                            area = round(rng.uniform(58, 142), 1)
                            status = rng.choices(["正常", "欠费", "报停"], [0.91, 0.06, 0.03])[0]
                            self.customers[cid] = {
                                "customer_id": cid, "name": rng.choice(_SURNAMES) + "".join(rng.sample(_GIVEN, rng.choice([1, 2]))),
                                "phone": phone, "address": f"{community}{k}号楼{u}单元{f}0{door}室",
                                "station_id": st.id, "building_id": bid, "floor": f, "top": f == floors,
                                "ground": f == 1, "edge": door == 1 and u in (1, units), "area_m2": area,
                                "account": {"status": status, "arrears_yuan": round(area * 28.5, 2) if status == "欠费" else 0.0},
                            }
                            self.by_phone[phone] = cid

    def lookup(self, key: str) -> dict | None:
        key = (key or "").strip()
        if key in self.customers:
            return self.customers[key]
        if key in self.by_phone:
            return self.customers[self.by_phone[key]]
        if len(key) >= 6:
            norm = re.sub(r"\s", "", key)
            for c in self.customers.values():
                if c["address"] == norm or c["address"].endswith(norm):
                    return c
        return None

    def room_temp(self, c: dict, station_tin: float) -> float:
        """住户估算室温 = 站典型室温 + 楼栋偏差 + 位置修正（顶层/首层/边户偏冷）。"""
        b = self.buildings[c["building_id"]]
        t = station_tin + b["temp_offset"] - (1.0 if c["top"] else 0) - (0.6 if c["ground"] else 0) - (0.4 if c["edge"] else 0)
        return round(t, 1)

    def customers_in(self, scope: str) -> list[dict]:
        if scope in self.buildings:
            return [c for c in self.customers.values() if c["building_id"] == scope]
        if scope in STATION_BY_ID:
            return [c for c in self.customers.values() if c["station_id"] == scope]
        if scope in SOURCE_BY_ID:
            return [c for c in self.customers.values() if STATION_BY_ID[c["station_id"]].source_id == scope]
        raise OpsError("BAD_SCOPE", f"范围 {scope} 无效（楼栋/热力站/热源 ID）")


# ================================================================ 运维服务
class OpsService:
    def __init__(self, engine, control, ai, kb, breaker, master: MasterData, data_dir: str | os.PathLike | None = None):
        self.engine, self.control, self.ai, self.kb, self.breaker = engine, control, ai, kb, breaker
        self.md = master
        self.data_dir = Path(data_dir or os.environ.get("HEATNET_DATA_DIR") or Path(__file__).resolve().parents[2] / "data")
        self.lock = threading.RLock()
        self.staff = [{"staff_id": i, "name": n, "team": t, "skills": sorted(sk), "base": b, "on_shift": True}
                      for i, n, t, sk, b in STAFF]
        self.workorders: dict[str, dict] = {}
        self.handoffs: dict[str, dict] = {}
        self.last_patrol: dict[str, str] = {}
        self._seed_history()
        self._load()

    # ---------- 持久化 ----------
    def _path(self) -> Path:
        return self.data_dir / "ops_state.json"

    def _load(self):
        p = self._path()
        if p.exists():
            d = json.loads(p.read_text(encoding="utf-8"))
            for w in d.get("workorders", []):
                self.workorders[w["workorder_id"]] = w
            for h in d.get("handoffs", []):
                self.handoffs[h["handoff_id"]] = h

    def persist(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._path().with_suffix(".tmp")
        tmp.write_text(json.dumps({"workorders": [w for w in self.workorders.values() if not w.get("seed")],
                                   "handoffs": list(self.handoffs.values())}, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self._path())

    # ---------- 演示历史工单 ----------
    def _seed_history(self):
        rng = random.Random(77)
        now = now_local()
        templates = [("不热", "户内暖气不热，室温偏低"), ("漏水", "暖气片接口滴水"), ("设备故障", "循环泵振动偏大"),
                     ("不热", "暖气上热下凉，需放气"), ("测量异常", "室温采集器离线"), ("过热", "室温偏高，要求调低")]
        for i in range(36):
            st = rng.choice(STATIONS)
            typ, desc = rng.choice(templates)
            created = now - timedelta(days=rng.uniform(1.5, 14))
            self._seed_wo(st.id, typ, desc, created, closed=True, rng=rng)
        # 近期热点：北站家园站同一楼栋 24 小时内多户不热；北苑小区站漏水待处理；一张超时工单
        b = f"ST10-B02"
        for h in (20, 9, 3):
            self._seed_wo("ST10", "不热", "家中暖气不热，室温约 17℃", now - timedelta(hours=h), closed=False, rng=rng,
                          building=b, assign=h > 10)
        self._seed_wo("ST07", "漏水", "楼道暖气管有渗水", now - timedelta(hours=5), closed=False, rng=rng, assign=True)
        w = self._seed_wo("ST05", "设备故障", "补水泵频繁启停", now - timedelta(hours=30), closed=False, rng=rng, assign=True)
        w["status"] = "处理中"
        for st in STATIONS:
            self.last_patrol[st.id] = (now - timedelta(days=rng.randint(1, 12))).strftime("%Y-%m-%d")

    def _seed_wo(self, sid, typ, desc, created, closed, rng, building=None, assign=True):
        building = building or rng.choice([b for b in self.md.buildings if b.startswith(sid)])
        cust = rng.choice([c for c in self.md.customers.values() if c["building_id"] == building]) if typ in ("不热", "过热", "漏水") else None
        prio = "紧急" if typ == "漏水" else "普通"
        w = self._new_wo(sid, typ, desc, prio, rng.choice(["热线", "小程序", "平台"]), "演示数据", created,
                         building_id=building, customer_id=cust["customer_id"] if cust else None)
        w["seed"] = True
        if assign:
            s = rng.choice([s for s in self.staff if WO_SKILLS[typ] & set(s["skills"])] or self.staff)
            w.update(status="已派单", assignee=s["staff_id"], assignee_name=s["name"], assigned_at=_iso(created + timedelta(minutes=20)))
        if closed:
            w.update(status="已完成", closed_at=_iso(created + timedelta(hours=rng.uniform(1, 20))), result="已处理，用户确认")
        self.workorders[w["workorder_id"]] = w
        return w

    def _new_wo(self, sid, typ, desc, prio, source, created_by, created: datetime, building_id=None, customer_id=None):
        return {"workorder_id": "WO" + uuid.uuid4().hex[:8].upper(), "station_id": sid,
                "station_name": STATION_BY_ID[sid].name, "building_id": building_id,
                "building_name": self.md.buildings[building_id]["name"] if building_id else None,
                "customer_id": customer_id, "type": typ, "description": desc, "priority": prio, "source": source,
                "status": "待派单", "assignee": None, "assignee_name": None, "created_by": created_by,
                "created_at": _iso(created), "due_at": _iso(created + timedelta(hours=PRIORITY_SLA_H[prio])),
                "assigned_at": None, "closed_at": None, "result": None, "flags": [], "log": [], "request_id": None}

    # ---------- 工单 ----------
    def _overdue(self, w: dict, now: datetime) -> bool:
        return w["status"] not in ("已完成", "已关闭") and w["due_at"] < _iso(now)

    def query(self, *, stations: set[str], status: str | None = None, typ: str | None = None, days: int = 7,
              object_id: str | None = None) -> dict:
        now = now_local()
        since = _iso(now - timedelta(days=days))
        rows = []
        for w in self.workorders.values():
            if w["station_id"] not in stations or w["created_at"] < since:
                continue
            if status == "未完成" and w["status"] in ("已完成", "已关闭"):
                continue
            if status and status != "未完成" and w["status"] != status:
                continue
            if typ and w["type"] != typ:
                continue
            if object_id and object_id not in (w["station_id"], w["building_id"], w["customer_id"],
                                               STATION_BY_ID[w["station_id"]].source_id):
                continue
            rows.append({**w, "overdue": self._overdue(w, now)})
        rows.sort(key=lambda w: w["created_at"], reverse=True)
        # 聚类：同一楼栋/站点 + 同类型；24 小时内同楼 3 户以上不热视为整楼问题（POL-02 §5、SVC-01 §2）
        groups = defaultdict(list)
        for w in rows:
            groups[(w["building_id"] or w["station_id"], w["type"])].append(w)
        clusters = []
        for (obj, t), ws in groups.items():
            if len(ws) < 2:
                continue
            recent = [w for w in ws if w["created_at"] >= _iso(now - timedelta(hours=24))]
            whole = t == "不热" and obj in self.md.buildings and len(recent) >= 3
            clusters.append({"object_id": obj, "object_name": self.md.buildings[obj]["name"] if obj in self.md.buildings
                             else STATION_BY_ID[obj].name, "type": t, "count": len(ws), "last_24h": len(recent),
                             "whole_building": whole,
                             "hint": "同一楼栋 24 小时内 3 户以上报不热，按整楼问题转调度" if whole else ""})
        clusters.sort(key=lambda c: (-c["whole_building"], -c["last_24h"], -c["count"]))
        st = Counter(w["status"] for w in rows)
        return {"workorders": rows, "total": len(rows), "by_status": dict(st), "by_type": dict(Counter(w["type"] for w in rows)),
                "overdue": sum(1 for w in rows if w["overdue"]), "clusters": clusters[:10], "days": days}

    def get(self, wid: str) -> dict:
        w = self.workorders.get(wid)
        if not w:
            raise OpsError("NOT_FOUND", f"工单 {wid} 不存在")
        return {**w, "overdue": self._overdue(w, now_local())}

    def create(self, *, object_id: str, typ: str, description: str, priority: str, source: str, created_by: str,
               customer_id: str | None = None, request_id: str | None = None, auto_assign: bool = True) -> tuple[dict, bool]:
        """返回 (工单, 是否新建)。同一住户/楼栋同类型未完成工单视为重复，返回已有工单。"""
        if typ not in WO_TYPES:
            raise OpsError("BAD_TYPE", f"工单类型须为 {WO_TYPES}")
        if priority not in PRIORITY_SLA_H:
            raise OpsError("BAD_PRIORITY", f"优先级须为 {list(PRIORITY_SLA_H)}")
        if len((description or "").strip()) < 4:
            raise OpsError("BAD_DESC", "请填写问题描述")
        building_id = None
        if customer_id:
            c = self.md.customers.get(customer_id)
            if not c:
                raise OpsError("NOT_FOUND", f"住户 {customer_id} 不存在")
            sid, building_id = c["station_id"], c["building_id"]
        elif object_id in self.md.buildings:
            sid, building_id = self.md.buildings[object_id]["station_id"], object_id
        elif object_id in STATION_BY_ID:
            sid = object_id
        else:
            raise OpsError("BAD_OBJECT", "工单对象须为住户、楼栋或热力站")
        with self.lock:
            for w in self.workorders.values():
                same = (customer_id and w["customer_id"] == customer_id) or (not customer_id and building_id and
                                                                         w["building_id"] == building_id and not w["customer_id"])
                if same and w["type"] == typ and w["status"] not in ("已完成", "已关闭"):
                    return w, False
            w = self._new_wo(sid, typ, description.strip()[:500], priority, source, created_by, now_local(),
                             building_id=building_id, customer_id=customer_id)
            w["flags"] = injection_flags(description)
            w["request_id"] = request_id
            w["log"].append({"ts": w["created_at"], "by": created_by, "action": "创建"})
            self.workorders[w["workorder_id"]] = w
            if auto_assign:
                cands = self.suggest(w["workorder_id"])["candidates"]
                if cands and cands[0]["score"] >= 0.6:
                    self._assign(w, cands[0]["staff_id"], "系统自动派单")
            self.persist()
            return w, True

    def _assign(self, w: dict, staff_id: str, by: str):
        s = next((s for s in self.staff if s["staff_id"] == staff_id), None)
        if not s:
            raise OpsError("NOT_FOUND", f"维修人员 {staff_id} 不存在")
        w.update(status="已派单", assignee=staff_id, assignee_name=s["name"], assigned_at=_iso(now_local()))
        w["log"].append({"ts": w["assigned_at"], "by": by, "action": f"派单给 {s['name']}（{s['team']}）"})

    def assign(self, wid: str, staff_id: str, by: str) -> dict:
        with self.lock:
            w = self.get(wid)
            w = self.workorders[wid]
            if w["status"] in ("已完成", "已关闭"):
                raise OpsError("BAD_STATE", "工单已结束")
            self._assign(w, staff_id, by)
            self.persist()
            return self.get(wid)

    def update_status(self, wid: str, status: str, by: str, result: str = "") -> dict:
        allowed = {"处理中": ("已派单",), "已完成": ("已派单", "处理中"), "已关闭": ("待派单", "已派单", "处理中")}
        with self.lock:
            w = self.workorders.get(wid)
            if not w:
                raise OpsError("NOT_FOUND", f"工单 {wid} 不存在")
            if status not in allowed or w["status"] not in allowed[status]:
                raise OpsError("BAD_STATE", f"工单状态 {w['status']} 不能变更为 {status}")
            if status in ("已完成", "已关闭") and len(result.strip()) < 2:
                raise OpsError("RESULT_REQUIRED", "结束工单须填写处理结果")
            w["status"] = status
            if status in ("已完成", "已关闭"):
                w["closed_at"], w["result"] = _iso(now_local()), result.strip()
            w["log"].append({"ts": _iso(now_local()), "by": by, "action": status + (f"：{result.strip()}" if result else "")})
            self.persist()
            return self.get(wid)

    # ---------- 派单推荐 ----------
    def suggest(self, wid: str) -> dict:
        """技能匹配 0.45 + 距离 0.30（exp(-d/5km)）+ 当前负载 0.25（1/(1+未完成工单数)）。"""
        w = self.workorders.get(wid)
        if not w:
            raise OpsError("NOT_FOUND", f"工单 {wid} 不存在")
        target = self.md.buildings[w["building_id"]]["xy"] if w["building_id"] else STATION_BY_ID[w["station_id"]].xy
        need = WO_SKILLS.get(w["type"], set())
        load = Counter(x["assignee"] for x in self.workorders.values() if x["assignee"] and x["status"] in ("已派单", "处理中"))
        out = []
        for s in self.staff:
            if not s["on_shift"]:
                continue
            skill = len(need & set(s["skills"])) / len(need) if need else 1.0
            d = _dist_km(STATION_BY_ID[s["base"]].xy, target)
            n = load.get(s["staff_id"], 0)
            score = 0.45 * skill + 0.30 * math.exp(-d / 5) + 0.25 / (1 + n)
            out.append({"staff_id": s["staff_id"], "name": s["name"], "team": s["team"], "skills": s["skills"],
                        "skill_match": round(skill, 2), "distance_km": round(d, 1), "open_orders": n,
                        "score": round(score, 3),
                        "reasons": [x for x in (f"技能匹配 {skill:.0%}" if need else "无特殊技能要求",
                                                f"距离约 {d:.1f} km", f"手上未完成 {n} 单") if x]})
        out.sort(key=lambda c: -c["score"])
        return {"workorder_id": wid, "required_skills": sorted(need), "candidates": out[:3],
                "method": "技能匹配×0.45 + 距离分 exp(-距离/5km)×0.30 + 负载分 1/(1+在办工单)×0.25"}

    # ---------- 巡检计划 ----------
    CHECK_ITEMS = {
        "MAKEUP": "巡查二网管沟与井室、核对补水泵启停记录（MAN-01 §2）",
        "TIN_LOW": "检查调节阀开度与楼栋入口阀，核对室温采集点（MAN-01 §3）",
        "UNDERSUPPLY": "测一次侧供回水压差、检查过滤器压降（MAN-01 §5）",
        "HEAT_BAL": "便携式测温仪比对一次回水温度、校验热量表（MAN-01 §6）",
        "P2R_LOW": "检查补水泵与补水箱水位（MAN-01 §8）",
        "COMM": "检查站内通讯设备与天线（MAN-01 §9）",
        "heat_balance": "比对一次回水测温点（疑似传感器漂移）",
        "makeup": "核对补水量变化、排查失水",
    }
    BASE_ITEMS = ["循环泵运行声音、振动与轴承温度", "换热器一二次侧进出口温度与压降", "补水箱液位与补水泵", "电动调节阀阀位反馈"]

    def patrol_plan(self, *, stations: list[str], date: str | None, focus: str = "风险优先", max_stops: int = 6) -> dict:
        if focus not in ("风险优先", "常规"):
            raise OpsError("BAD_FOCUS", "focus 须为 风险优先 / 常规")
        date = date or now_local().strftime("%Y-%m-%d")
        alarms = self.engine.active_alarms()
        an = {e["station_id"]: e for e in self.ai.anomalies()["data"]["events"] if not e["known_cause"]}
        open_wo = Counter(w["station_id"] for w in self.workorders.values() if w["status"] not in ("已完成", "已关闭"))
        today = datetime.strptime(date, "%Y-%m-%d")
        scored = []
        for sid in stations:
            st_alarms = [a for a in alarms if a["object_id"] == sid]
            days = (today - datetime.strptime(self.last_patrol.get(sid, date), "%Y-%m-%d")).days
            score = sum({1: 3, 2: 2, 3: 1}[a["level"]] for a in st_alarms) + (3 if sid in an else 0) \
                + open_wo.get(sid, 0) + (2 if self.breaker.is_tripped(sid) else 0) + max(days, 0) / 7
            basis = [f"{a['level']}级报警：{a['message']}" for a in st_alarms]
            if sid in an:
                basis.append(f"异常检测：{an[sid]['type']}")
            if open_wo.get(sid):
                basis.append(f"未完成工单 {open_wo[sid]} 张")
            if self.breaker.is_tripped(sid):
                basis.append("所在管网熔断中")
            basis.append(f"距上次巡检 {days} 天")
            items = list(dict.fromkeys([self.CHECK_ITEMS[a["code"]] for a in st_alarms if a["code"] in self.CHECK_ITEMS]
                                       + ([self.CHECK_ITEMS[an[sid]["feature"]]] if sid in an and an[sid]["feature"] in self.CHECK_ITEMS else [])))
            scored.append({"station_id": sid, "station_name": STATION_BY_ID[sid].name, "risk_score": round(score, 2),
                           "basis": basis, "must_check": items + self.BASE_ITEMS})
        scored.sort(key=lambda s: -s["risk_score"])
        stops = [s for s in scored if focus == "常规" or s["risk_score"] >= 1][:max_stops if focus == "风险优先" else None]
        # 路线：从片区热源出发最近邻排序
        start_src = STATION_BY_ID[stops[0]["station_id"]].source_id if stops else SOURCES[0].id
        pos, route, total = SOURCE_BY_ID[start_src].xy, [], 0.0
        left = stops[:]
        while left:
            nxt = min(left, key=lambda s: math.dist(pos, STATION_BY_ID[s["station_id"]].xy))
            d = _dist_km(pos, STATION_BY_ID[nxt["station_id"]].xy)
            total += d
            route.append({**nxt, "leg_km": round(d, 1)})
            pos = STATION_BY_ID[nxt["station_id"]].xy
            left.remove(nxt)
        minutes = total / TRAVEL_KMH * 60 + VISIT_MIN * len(route)
        return {"plan_id": "PT" + uuid.uuid4().hex[:6].upper(), "date": date, "focus": focus, "start": SOURCE_BY_ID[start_src].name,
                "route": route, "distance_km": round(total, 1), "est_hours": round(minutes / 60, 1),
                "skipped": [s["station_name"] for s in scored if s not in stops],
                "method": "风险分 = 一/二/三级报警各 3/2/1 + 异常检测 3 + 未完成工单数 + 熔断 2 + 距上次巡检天数/7；"
                          "路线按最近邻从片区热源出发排序（近似最短路线）"}

    # ---------- 交接班 ----------
    @staticmethod
    def shift_window(shift_id: str | None) -> tuple[str, datetime, datetime]:
        now = now_local()
        if not shift_id:
            day = now.date() if now.hour >= 8 else (now - timedelta(days=1)).date()
            kind = "D" if 8 <= now.hour < 20 else "N"
            shift_id = f"{day:%Y-%m-%d}-{kind}"
        m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})-([DN])", shift_id)
        if not m:
            raise OpsError("BAD_SHIFT", "班次格式为 YYYY-MM-DD-D（白班 08–20）或 -N（夜班 20–次日 08）")
        start = datetime.strptime(m.group(1), "%Y-%m-%d") + timedelta(hours=8 if m.group(2) == "D" else 20)
        return shift_id, start, start + timedelta(hours=12)

    def handover(self, shift_id: str | None, stations: set[str], sources: set[str], weather: dict, service_requests: list[dict]) -> dict:
        shift_id, start, end = self.shift_window(shift_id)
        s, e = _iso(start), _iso(end)
        vis = lambda oid: oid in stations or oid in sources  # noqa: E731
        alarms = [a for a in self.engine.alarm_log if s <= a["first_ts"] < e and vis(a["object_id"])]
        active = [a for a in self.engine.active_alarms() if vis(a["object_id"])]
        cmds = [c for c in self.control.list_commands() if s <= c["executed_at"] < e and c["station_id"] in stations]
        pend = [d for d in self.control.pending_approvals() if d["station_id"] in stations]
        wos = [w for w in self.workorders.values() if w["station_id"] in stations]
        created = [w for w in wos if s <= w["created_at"] < e]
        closed = [w for w in wos if w["closed_at"] and s <= w["closed_at"] < e]
        now = now_local()
        open_overdue = [w for w in wos if self._overdue(w, now)]
        trips = [(net, ev) for net in sources for ev in self.breaker.nets[net].events if s <= ev["ts"] < e]
        tripped = [net for net in sources if self.breaker.is_tripped(net)]
        hand = [h for h in self.handoffs.values() if h["status"] != "已关闭" and h["station_id"] in stations]
        reqs = [r for r in service_requests if s <= r["created_at"] < e]
        notes = []
        for a in active:
            if a["level"] == 1:
                notes.append(f"一级报警未恢复：{a['object_name']} {a['message']}")
        notes += [f"超时工单：{w['workorder_id']} {w['station_name']} {w['type']}（应于 {w['due_at'][5:16]} 前到场）" for w in open_overdue]
        notes += [f"待审批调控：{d['station_name']} {d['param_text']}（{d['approval_request']['approval_id']}）" for d in pend]
        notes += [f"{SOURCE_BY_ID[n].name}管网熔断中，需复核后解除" for n in tripped]
        notes += [f"未结移交：{h['summary']}" for h in hand]
        notes += list(weather.get("warnings", []))
        if not notes:
            notes.append("无特别注意事项")
        summary = (f"{shift_id} 班：新增报警 {len(alarms)} 条（当前活动 {len(active)} 条），下发调控 {len(cmds)} 次"
                   f"（回滚 {sum(1 for c in cmds if c['status'] == 'rolled_back')} 次），新建工单 {len(created)} 张、完结 {len(closed)} 张，"
                   f"客服诉求 {len(reqs)} 件；超时工单 {len(open_overdue)} 张，待审批 {len(pend)} 件。")
        return {"shift_id": shift_id, "start": s, "end": e, "summary": summary,
                "alarms": [{k: a[k] for k in ("object_name", "level", "message", "first_ts", "active")} for a in alarms[:30]],
                "commands": [{k: c[k] for k in ("command_id", "station_name", "description", "executed_by", "status")} for c in cmds],
                "pending_approvals": [{"draft_id": d["draft_id"], "station_name": d["station_name"], "description": d["description"]} for d in pend],
                "workorders_created": [{k: w[k] for k in ("workorder_id", "station_name", "type", "status")} for w in created],
                "workorders_closed": [{k: w[k] for k in ("workorder_id", "station_name", "type", "result")} for w in closed],
                "overdue": [{k: w[k] for k in ("workorder_id", "station_name", "type", "due_at", "assignee_name")} for w in open_overdue],
                "breaker_events": [{"network": SOURCE_BY_ID[n].name, **ev} for n, ev in trips],
                "open_handoffs": hand, "service_requests": len(reqs), "notes": notes,
                "weather": weather.get("summary", "")}

    # ---------- 移交（客服 → 调度） ----------
    def handoff(self, *, station_id: str, building_id: str | None, summary: str, request_id: str | None, by: str) -> dict:
        with self.lock:
            for h in self.handoffs.values():
                if h["status"] != "已关闭" and h["station_id"] == station_id and h["building_id"] == building_id:
                    h["related_requests"] = sorted(set(h["related_requests"]) | ({request_id} if request_id else set()))
                    self.persist()
                    return h
            h = {"handoff_id": "HO" + uuid.uuid4().hex[:6].upper(), "to": "调度", "from": by, "station_id": station_id,
                 "station_name": STATION_BY_ID[station_id].name, "building_id": building_id,
                 "building_name": self.md.buildings[building_id]["name"] if building_id else None,
                 "summary": summary.strip()[:300], "related_requests": [request_id] if request_id else [],
                 "status": "待接收", "created_at": _iso(now_local()), "log": [], "flags": injection_flags(summary)}
            self.handoffs[h["handoff_id"]] = h
            self.persist()
            return h

    def update_handoff(self, hid: str, status: str, by: str, note: str = "") -> dict:
        with self.lock:
            h = self.handoffs.get(hid)
            if not h:
                raise OpsError("NOT_FOUND", f"移交 {hid} 不存在")
            order = {"待接收": 0, "已接收": 1, "已关闭": 2}
            if status not in order or order[status] <= order[h["status"]]:
                raise OpsError("BAD_STATE", f"移交状态 {h['status']} 不能变更为 {status}")
            if status == "已关闭" and len(note.strip()) < 2:
                raise OpsError("NOTE_REQUIRED", "关闭移交须填写处理结果")
            h["status"] = status
            h["log"].append({"ts": _iso(now_local()), "by": by, "action": status, "note": note})
            self.persist()
            return h
