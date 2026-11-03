"""报告生成（设计文档第八节 doc_generate_report，R1）：运行日报、运行周报、节能分析报告、事故报告。

原则：报告里的数字由程序计算，不由模型书写。
  ① 取数：调用现有统计、AI、报警、调控、审计、工单、客服服务，按调用者的数据范围过滤；
  ② 结论：按固定口径计算（同比环比、达标率、度日数修正、节能量分解、双重差分）；
  ③ 成文：模板拼句，句中每个数字都经 Facts.n() 登记（值、单位、来源）；
  ④ 校验：把正文、要点、口径说明里出现的每个数字与登记表核对，查不到的数字视为未经核实，报告不予生成；
  ⑤ 输出：结构化报告（章节、表格、图表）+ 自包含 HTML（可打印为 PDF），返回链接和关键结论。
表格单元格直接来自计算结果，不经人手，所以不参与文本校验；事故报告的原因结论须人工审核后填写。
"""
from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import threading
import uuid
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from ..config import GJ_TO_KGCE, KGCE_TO_KGCO2, SOURCE_BY_ID, SOURCES, STATION_BY_ID, STATIONS, T_BALANCE, T_ROOM_TARGET
from ..sim.history import ADOPTIONS, TZ, now_local
from .analytics import YEAR, area_of, name_of
from .weather_svc import weather_report

REPORT_TYPES = {"daily": "运行日报", "weekly": "运行周报", "energy": "节能分析报告", "incident": "事故报告"}
STD_TIN, LOW_TIN, HIGH_TIN = 18.0, 18.5, 21.5          # POL-02 §1 / §4
MAX_ENERGY_DAYS = 60
DEDUPE_MIN = 10
CN_NUM = "一二三四五六七八九十"
CONTENT_PERMS = ("ops:read", "service:read", "ai:read")
NUM = re.compile(r"(?<![\d.])-?\d+(?:\.\d+)?(?![\d.])")


class ReportError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


# ================================================================ 事实登记与核对
class Facts:
    """正文中出现的每个数字都通过 n() 写入；lit() 登记含数字的非数值文本（日期、编号、名称、引文）。"""

    def __init__(self):
        self.items: dict[str, dict] = {}
        self.nums: set[str] = set()
        self.lits: set[str] = set()

    def n(self, key: str, value, nd: int = 1, unit: str = "", source: str = "") -> str:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return "—"
        v = float(value)
        text = f"{v:.{nd}f}" if nd > 0 else f"{int(round(v))}"
        if float(text) == 0:
            text = text.lstrip("-")
        k, i = key, 2
        while k in self.items and self.items[k]["text"] != text:
            k, i = f"{key}#{i}", i + 1
        self.items[k] = {"value": round(v, 6), "text": text, "unit": unit, "source": source}
        self.nums.add(text)
        return text

    def lit(self, s) -> str:
        s = str(s)
        if any(c.isdigit() for c in s):
            self.lits.add(s)
        return s

    def verify(self, texts: list[str]) -> dict:
        unmatched, checked = [], 0
        lits = sorted(self.lits, key=len, reverse=True)
        for t in texts:
            for l in lits:
                t = t.replace(l, " ")
            for m in NUM.finditer(t):
                checked += 1
                if m.group(0) not in self.nums:
                    unmatched.append({"number": m.group(0), "context": t[max(0, m.start() - 12):m.end() + 12].strip()})
        return {"ok": not unmatched, "numbers_checked": checked, "facts_registered": len(self.items),
                "unmatched": unmatched[:20],
                "rule": "正文、要点与口径说明中的每个数字都必须来自登记的计算结果、规范常数或原始记录"}


def report_texts(rep: dict) -> list[str]:
    out = list(rep["key_findings"]) + list(rep.get("method", []))
    for s in rep["sections"]:
        if s.get("human_written"):
            continue
        out.append(s["heading"])
        out += s.get("paragraphs", [])
    return out


def _chg(F: Facts, key: str, v, label: str) -> str:
    if v is None:
        return ""
    if abs(v) < 0.05:
        return f"，{label}基本持平"
    return f"，{label}{'增加' if v > 0 else '减少'} {F.n(key, abs(v), 1, '%')}%"


def norm_change(cur: dict, base: dict) -> float | None:
    """修正单耗变化 %：直接由热量与度日数计算（面积相同可约去），避免用四舍五入后的单耗相除带来的偏差。"""
    if not cur.get("heat_gj") or not base.get("heat_gj") or not cur.get("hdd") or not base.get("hdd") \
            or cur["hdd"] < 0.5 or base["hdd"] < 0.5:
        return None
    return ((cur["heat_gj"] / cur["hdd"]) / (base["heat_gj"] / base["hdd"]) - 1) * 100


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _r(v, n=2):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return v
    return None if math.isnan(f) or math.isinf(f) else round(f, n)


# ================================================================ 服务
class ReportService:
    def __init__(self, analytics, ai, engine, control, breaker, ops, service, kb, data_dir=None):
        self.analytics, self.ai, self.engine, self.control = analytics, ai, engine, control
        self.breaker, self.ops, self.service, self.kb = breaker, ops, service, kb
        self.store = analytics.store
        base = Path(data_dir or os.environ.get("HEATNET_DATA_DIR") or Path(__file__).resolve().parents[2] / "data")
        self.dir = base / "reports"
        self.lock = threading.RLock()
        self.reports: dict[str, dict] = {}
        if self.dir.exists():
            for p in self.dir.glob("*.json"):
                try:
                    r = json.loads(p.read_text(encoding="utf-8"))
                    self.reports[r["report_id"]] = r
                except (ValueError, KeyError):
                    continue

    # ---------- 范围 ----------
    @staticmethod
    def stations_in(scope: str, sources) -> list[str]:
        if scope in STATION_BY_ID:
            return [scope]
        if scope in SOURCE_BY_ID:
            return [s.id for s in STATIONS if s.source_id == scope]
        return [s.id for s in STATIONS if sources is None or s.source_id in sources]

    @staticmethod
    def nets_in(scope: str, sources) -> list[str]:
        if scope in STATION_BY_ID:
            return [STATION_BY_ID[scope].source_id]
        if scope in SOURCE_BY_ID:
            return [scope]
        return [s.id for s in SOURCES if sources is None or s.id in sources]

    def summ(self, oid: str, start: datetime, end: datetime, sources=None) -> dict:
        a = self.analytics
        return a._summary(oid, a._slice(oid, start, end, sources), sources)

    @staticmethod
    def compliance(rows: list[dict]) -> tuple[float | None, int, int]:
        hours = sum(r.get("hours", 0) for r in rows if r.get("tin_low_hours") is not None)
        low = sum(r.get("tin_low_hours", 0) for r in rows if r.get("tin_low_hours") is not None)
        return ((hours - low) / hours if hours else None), hours, low

    def started_at(self) -> datetime:
        return datetime.fromtimestamp(self.engine.started, TZ).replace(tzinfo=None)

    def events(self, start: datetime, end: datetime, stations: list[str], nets: list[str]) -> dict:
        s, e = _iso(start), _iso(end)
        objs = set(stations) | set(nets)
        cmds = [c for c in self.control.commands.values() if c["station_id"] in stations and s <= c["executed_at"] < e]
        rbs = [c for c in self.control.commands.values() if c["station_id"] in stations and c.get("rollback")
               and s <= c["rollback"]["ts"] < e]
        return {
            "alarms": [a for a in self.engine.alarm_log if a["object_id"] in objs and s <= a["first_ts"] < e],
            "commands": cmds, "rollbacks": rbs,
            "trips": [(n, ev) for n in nets for ev in self.breaker.nets[n].events if ev["action"] == "trip" and s <= ev["ts"] < e],
            "wo_created": [w for w in self.ops.workorders.values() if w["station_id"] in stations and s <= w["created_at"] < e],
            "wo_closed": [w for w in self.ops.workorders.values() if w["station_id"] in stations and w["closed_at"]
                          and s <= w["closed_at"] < e],
            "requests": [r for r in self.service.requests.values() if r["station_id"] in stations and s <= r["created_at"] < e],
        }

    def memory_warning(self, start: datetime) -> list[str]:
        st = self.started_at()
        if start < st:
            return [f"报警、调控、审计与熔断记录自本次系统启动（{st:%m-%d %H:%M}）起保存在内存中，此前的记录不在本报告内；"
                    "运行数据、工单不受影响"]
        return []

    # ---------- 期间 ----------
    def parse_period(self, rtype: str, period: str | None) -> tuple[datetime, datetime, str]:
        today = now_local().date()
        p = (period or "").strip()
        try:
            if rtype == "daily":
                d = today - timedelta(days=1) if p in ("", "yesterday", "昨天") else today if p in ("today", "今天") \
                    else date.fromisoformat(p)
                s = datetime.combine(d, datetime.min.time())
                return s, s + timedelta(days=1), d.isoformat()
            if "~" in p:
                a, b = (date.fromisoformat(x.strip()) for x in p.split("~", 1))
            elif p in ("", "last7d", "上周", "近7天"):
                b = today - timedelta(days=1)
                a = b - timedelta(days=6)
            else:
                b = date.fromisoformat(p)
                a = b - timedelta(days=6)
        except ValueError:
            raise ReportError("BAD_PERIOD", "period 格式：日报 YYYY-MM-DD；周报/节能报告 YYYY-MM-DD~YYYY-MM-DD（含两端）"
                                            "或结束日期 YYYY-MM-DD（向前 7 天）") from None
        if b < a:
            raise ReportError("BAD_PERIOD", "结束日期早于开始日期")
        days = (b - a).days + 1
        if rtype == "weekly" and not 2 <= days <= 14:
            raise ReportError("BAD_PERIOD", "周报期间应为 2–14 天")
        if rtype == "energy" and days > MAX_ENERGY_DAYS:
            raise ReportError("BAD_PERIOD", f"节能报告期间不超过 {MAX_ENERGY_DAYS} 天")
        if b > today:
            raise ReportError("BAD_PERIOD", "期间不能晚于今天")
        s = datetime.combine(a, datetime.min.time())
        return s, datetime.combine(b, datetime.min.time()) + timedelta(days=1), f"{a.isoformat()}~{b.isoformat()}"

    # ---------- 入口 ----------
    def generate(self, *, rtype: str, scope: str, period: str | None, event_id: str | None, sources, by: str,
                 actor_kind: str, can_see, has=lambda perm: True, force: bool = False) -> dict:
        if rtype not in REPORT_TYPES:
            raise ReportError("BAD_TYPE", f"report_type 须为 {list(REPORT_TYPES)}")
        if rtype == "incident":
            if not event_id:
                raise ReportError("EVENT_REQUIRED", "事故报告须给出 event_id（报警 ID 或指令号）")
            start = end = None
            label = event_id.strip()
        else:
            start, end, label = self.parse_period(rtype, period)
        # 复用键包含影响内容的权限：同一参数下，权限不同的人看到的章节不同，不能共用一份报告
        perms = tuple(x for x in CONTENT_PERMS if has(x))
        key = (rtype, scope, label, tuple(sorted(sources)) if sources else None, perms)
        with self.lock:
            if not force:
                cutoff = _iso(now_local() - timedelta(minutes=DEDUPE_MIN))
                for r in self.reports.values():
                    if r["_key"] == list(map(lambda x: list(x) if isinstance(x, tuple) else x, key)) and r["generated_at"] >= cutoff \
                            and r["status"] != "已作废":
                        return {**r, "reused": True}
        F = Facts()
        ctx = {"F": F, "scope": scope, "sources": sources, "start": start, "end": end, "label": label,
               "scope_name": name_of(scope, sources), "can_see": can_see, "has": has}
        builder = {"daily": self._daily, "weekly": self._weekly, "energy": self._energy, "incident": self._incident}[rtype]
        rep = builder(ctx, event_id) if rtype == "incident" else builder(ctx)
        rep.update({"report_id": "RP" + uuid.uuid4().hex[:8].upper(), "type": rtype, "type_name": REPORT_TYPES[rtype],
                    "generated_at": _iso(now_local()), "generated_by": by, "actor_kind": actor_kind,
                    "facts": F.items, "literals": sorted(F.lits), "review": None,
                    "_key": list(map(lambda x: list(x) if isinstance(x, tuple) else x, key))})
        rep.setdefault("scope_id", scope)
        rep.setdefault("scope_name", ctx["scope_name"])
        rep.setdefault("status", "已生成")
        rep["sections"] = [x for x in rep["sections"] if x.get("paragraphs") or x.get("table") or x.get("chart")]
        for i, sec in enumerate(rep["sections"]):                 # 按权限省略章节后重新编号
            sec["heading"] = CN_NUM[i] + "、" + re.sub(r"^[一二三四五六七八九十]+、", "", sec["heading"])
        rep["check"] = F.verify(report_texts(rep))
        if not rep["check"]["ok"]:
            raise ReportError("FACT_CHECK_FAILED", "报告中有未经核实的数字，未生成：" +
                              "；".join(f"{u['number']}（…{u['context']}…）" for u in rep["check"]["unmatched"][:5]))
        payload = json.dumps({"facts": F.items, "tables": [s.get("table") for s in rep["sections"]]},
                             ensure_ascii=False, sort_keys=True, default=str)
        rep["fingerprint"] = hashlib.sha256(payload.encode()).hexdigest()[:16]
        with self.lock:
            self.reports[rep["report_id"]] = rep
            self._save(rep)
        return {**rep, "reused": False}

    def _save(self, rep: dict):
        self.dir.mkdir(parents=True, exist_ok=True)
        p = self.dir / f"{rep['report_id']}.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(rep, ensure_ascii=False, default=str), encoding="utf-8")
        os.replace(tmp, p)

    def get(self, rid: str) -> dict:
        r = self.reports.get(rid)
        if not r:
            raise ReportError("NOT_FOUND", f"报告 {rid} 不存在")
        return r

    @staticmethod
    def reverify(rep: dict) -> dict:
        """用报告保存的登记数据重新核对正文（报告被修改或转存后可复核）。"""
        F = Facts()
        F.items = rep["facts"]
        F.nums = {v["text"] for v in rep["facts"].values()}
        F.lits = set(rep.get("literals", []))
        return F.verify(report_texts(rep))

    def review(self, rid: str, *, by: str, approve: bool, conclusion: str) -> dict:
        with self.lock:
            r = self.get(rid)
            if r["type"] != "incident":
                raise ReportError("BAD_TYPE", "只有事故报告需要审核")
            if r["status"] != "草稿（待审核）":
                raise ReportError("BAD_STATE", f"报告状态为 {r['status']}，不能重复审核")
            if approve and len((conclusion or "").strip()) < 6:
                raise ReportError("CONCLUSION_REQUIRED", "审核通过须填写原因与整改结论")
            r["review"] = {"by": by, "at": _iso(now_local()), "approve": approve, "conclusion": (conclusion or "").strip()[:2000]}
            r["status"] = "已审核" if approve else "已作废"
            if approve:
                r["sections"].append({"heading": CN_NUM[len(r["sections"])] + "、原因与整改结论（人工审核）", "human_written": True,
                                      "paragraphs": [r["review"]["conclusion"], f"审核人：{by}　审核时间：{r['review']['at']}"]})
            self._save(r)
            return r

    # ================================================================ 日报
    def _daily(self, ctx) -> dict:
        F, scope, sources = ctx["F"], ctx["scope"], ctx["sources"]
        day = ctx["start"].date()
        d = self.analytics.daily(day, scope, sources)
        s, c = d["summary"], d["compare"]
        prev_s = self.summ(scope, ctx["start"] - timedelta(days=1), ctx["start"], sources)
        ly_s = self.summ(scope, ctx["start"] - YEAR, ctx["end"] - YEAR, sources)
        n_dod, n_yoy = norm_change(s, prev_s), norm_change(s, ly_s)
        name = F.lit(ctx["scope_name"])
        dl = F.lit(day.isoformat())
        warnings = []
        if d["partial"]:
            warnings.append("当日尚未结束，数据截至生成时刻")
        if s.get("hours", 0) < 20 and not d["partial"]:
            warnings.append("当日有效小时数不足 20 小时，结论仅供参考")
        p1 = (f"{dl}，{name}室外平均气温 {F.n('tout_avg', s['tout_avg'], 1, '℃')}℃，采暖度日数 {F.n('hdd', s['hdd'], 2, '℃·d')} ℃·d。"
              f"供热量 {F.n('heat_gj', s['heat_gj'], 1, 'GJ')} GJ" + _chg(F, "heat_dod", c["heat_gj"]["dod"], "较前一日")
              + _chg(F, "heat_yoy", c["heat_gj"]["yoy"], "较去年同日")
              + f"；负荷峰值 {F.n('peak_mw', d['peak']['heat_mw'], 2, 'MW')} MW，出现在 {F.lit(d['peak']['time'])}。")
        p2 = ""
        if s.get("norm_heat"):
            p2 = (f"度日数修正单耗 {F.n('norm_heat', s['norm_heat'], 1, 'kJ/(m²·℃·d)')} kJ/(m²·℃·d)"
                  + _chg(F, "norm_dod", n_dod, "较前一日") + _chg(F, "norm_yoy", n_yoy, "较去年同日")
                  + "。修正单耗 = 热量 ÷（面积 × 度日数），剔除了气温差异，比直接比较热量更能反映运行水平。")
        p3 = f"耗电 {F.n('elec_kwh', s['elec_kwh'], 0, 'kWh')} kWh，补水 {F.n('water_t', s['water_t'], 1, 't')} t。"
        sections = [{"heading": "一、运行概况", "paragraphs": [x for x in (p1, p2, p3) if x],
                     "chart": {"type": "line", "title": "逐时负荷与室外温度", "x": d["series"]["ts"],
                               "series": [{"name": "热负荷 MW", "data": d["series"]["heat_mw"]},
                                          {"name": "室外温度 ℃", "data": d["series"]["tout"], "axis": "right"}]}}]
        st_rows = [r for r in d["rows"] if r["id"] in STATION_BY_ID and r.get("hours")] or ([s] if scope in STATION_BY_ID else [])
        findings = []
        if st_rows:
            rate, hours, low = self.compliance(st_rows)
            std = F.n("std_tin", STD_TIN, 0, "℃", "POL-02 §1")
            paras = []
            if rate is not None:
                paras.append(f"室温达标率 {F.n('compliance', rate * 100, 1, '%')}%（典型室温不低于 {std}℃ 的小时数占比，"
                             f"{F.lit('POL-02 §3')}），统计 {F.n('st_hours', hours, 0, 'h')} 个站时。")
            low_st = [r for r in st_rows if r.get("tin_avg") is not None and r["tin_avg"] < LOW_TIN]
            high_st = [r for r in st_rows if r.get("tin_avg") is not None and r["tin_avg"] > HIGH_TIN]
            lo_t, hi_t = F.n("low_line", LOW_TIN, 1, "℃", "POL-02 §4"), F.n("high_line", HIGH_TIN, 1, "℃", "POL-02 §4")
            if low_st:
                paras.append(f"平均室温低于 {lo_t}℃ 的站：" + "、".join(
                    f"{F.lit(r['name'])}（{F.n('tin_' + r['id'], r['tin_avg'], 2, '℃')}℃）" for r in low_st) + "，需排查资用压差与阀门。")
                findings.append(f"{F.n('n_low', len(low_st), 0)} 个站平均室温偏低：" + "、".join(F.lit(r["name"]) for r in low_st) + "。")
            if high_st:
                paras.append(f"平均室温高于 {hi_t}℃ 的站：" + "、".join(
                    f"{F.lit(r['name'])}（{F.n('tin_' + r['id'], r['tin_avg'], 2, '℃')}℃）" for r in high_st) + "，存在过量供热。")
                findings.append(f"{F.n('n_high', len(high_st), 0)} 个站过量供热：" + "、".join(F.lit(r["name"]) for r in high_st) + "。")
            if not low_st and not high_st:
                paras.append(f"各站平均室温均在 {lo_t}～{hi_t}℃ 之间。")
            if rate is not None:
                findings.insert(0, f"室温达标率 {F.n('compliance', rate * 100, 1)}%。")
            waters = [r["unit_water"] for r in st_rows if r.get("unit_water") is not None]
            if len(waters) >= 3:
                med = float(np.median(waters))
                top = max(st_rows, key=lambda r: r.get("unit_water") or 0)
                if med > 0 and top["unit_water"] > 3 * med:
                    t = (f"{F.lit(top['name'])}单位面积补水 {F.n('water_top', top['unit_water'], 1, 'kg/千m²')} kg/千m²，"
                         f"为各站中位数的 {F.n('water_ratio', top['unit_water'] / med, 1, '倍')} 倍，疑似失水。")
                    paras.append(t)
                    findings.append(t)
            sections.append({"heading": "二、室温与供热质量", "paragraphs": paras,
                             "table": {"columns": [["name", "热力站"], ["heat_gj", "供热量 GJ"], ["norm_heat", "修正单耗"],
                                                   ["tin_avg", "平均室温 ℃"], ["compliance", "达标率 %"], ["unit_water", "补水 kg/千m²"]],
                                       "rows": [{"name": r["name"], "heat_gj": r.get("heat_gj"), "norm_heat": r.get("norm_heat"),
                                                 "tin_avg": r.get("tin_avg"),
                                                 "compliance": _r((1 - r["tin_low_hours"] / r["hours"]) * 100, 1) if r.get("hours") else None,
                                                 "unit_water": r.get("unit_water")} for r in st_rows]}})
        src_rows = [r for r in d["rows"] if r["id"] in SOURCE_BY_ID and r.get("hours")]
        if src_rows:
            sections.append({"heading": "三、热源出力", "paragraphs": [],
                             "table": {"columns": [["name", "热源"], ["heat_gj", "供热量 GJ"], ["heat_mw_max", "峰值 MW"],
                                                   ["load_rate_max", "最大负荷率 %"], ["unit_cost", "单位成本 元/GJ"]],
                                       "rows": [{k: r.get(k) for k in ("name", "heat_gj", "heat_mw_max", "load_rate_max", "unit_cost")}
                                                for r in src_rows]}})
        ev_par, ev_find = self._events_paragraphs(F, ctx["start"], ctx["end"], scope, sources, ctx["has"])
        sections.append({"heading": "四、报警、调控与工单", "paragraphs": ev_par})
        findings += ev_find
        if s.get("norm_heat") and n_dod is not None:
            findings.insert(0, f"供热量 {F.n('heat_gj', s['heat_gj'], 1)} GJ，修正单耗" +
                            ("基本持平" if abs(n_dod) < 0.05 else
                             f"较前一日{'上升' if n_dod > 0 else '下降'} {F.n('norm_dod', abs(n_dod), 1)}%") + "。")
        warnings += self.memory_warning(ctx["start"])
        return {"title": f"{ctx['scope_name']}运行日报（{day.isoformat()}）", "period": {"start": _iso(ctx["start"]),
                "end": _iso(ctx["end"]), "label": ctx["label"]}, "sections": sections, "key_findings": findings[:5],
                "warnings": warnings, "method": [f"度日数 HDD = Σ max({F.n('t_balance', T_BALANCE, 0, '℃', '口径常数')} − 逐时室外温度, 0) ÷ {F.n('h24', 24, 0, 'h')}。",
                                                  "达标率按站时统计：各站典型室温达标小时数之和 ÷ 有效小时数之和。"]}

    def _events_paragraphs(self, F: Facts, start, end, scope, sources, has) -> tuple[list[str], list[str]]:
        """报警/调控/熔断属于监控数据；工单、客服诉求只在调用者有相应查看权限时写入。"""
        ev = self.events(start, end, self.stations_in(scope, sources), self.nets_in(scope, sources))
        lv = Counter(a["level"] for a in ev["alarms"])
        paras = [f"新发报警 {F.n('alarms', len(ev['alarms']), 0)} 条（一级 {F.n('alarm_l1', lv.get(1, 0), 0)}、二级 "
                 f"{F.n('alarm_l2', lv.get(2, 0), 0)}、三级 {F.n('alarm_l3', lv.get(3, 0), 0)}）；下发调控 "
                 f"{F.n('commands', len(ev['commands']), 0)} 次，回滚 {F.n('rollbacks', len(ev['rollbacks']), 0)} 次；"
                 f"管网熔断 {F.n('trips', len(ev['trips']), 0)} 次。"]
        if has("ops:read"):
            paras.append(f"新建工单 {F.n('wo_created', len(ev['wo_created']), 0)} 张、完结 {F.n('wo_closed', len(ev['wo_closed']), 0)} 张。")
        if has("service:read"):
            paras.append(f"客服诉求 {F.n('requests', len(ev['requests']), 0)} 件。")
        by_type = Counter(w["type"] for w in ev["wo_created"])
        if by_type and has("ops:read"):
            paras.append("工单类型：" + "、".join(f"{t} {F.n('wo_' + t, n, 0)} 张" for t, n in by_type.most_common()) + "。")
        findings = []
        if lv.get(1):
            findings.append(f"发生一级报警 {F.n('alarm_l1', lv[1], 0)} 条：" + "、".join(
                F.lit(a["object_name"]) for a in ev["alarms"] if a["level"] == 1) + "。")
        if ev["rollbacks"] or ev["trips"]:
            findings.append(f"调控回滚 {F.n('rollbacks', len(ev['rollbacks']), 0)} 次、熔断 {F.n('trips', len(ev['trips']), 0)} 次，需复盘。")
        return paras, findings

    # ================================================================ 周报
    def _weekly(self, ctx) -> dict:
        F, scope, sources, start, end = ctx["F"], ctx["scope"], ctx["sources"], ctx["start"], ctx["end"]
        days = (end - start).days
        cur = self.summ(scope, start, end, sources)
        if not cur.get("hours"):
            raise ReportError("NO_DATA", "期间内无运行数据")
        prev = self.summ(scope, start - timedelta(days=days), start, sources)
        ly = self.summ(scope, start - YEAR, end - YEAR, sources)
        name = F.lit(ctx["scope_name"])
        warnings = []
        if prev.get("hours", 0) < days * 24 * 0.9:
            prev = {}
            warnings.append("上一周期运行数据不完整（本采暖季开始不久），不做环比")
        def pct(a, b):
            return (a - b) / b * 100 if a is not None and b else None
        p1 = (f"{F.lit(ctx['label'])}（{F.n('days', days, 0, '天')} 天），{name}室外平均气温 {F.n('tout', cur['tout_avg'], 1, '℃')}℃，"
              f"度日数 {F.n('hdd', cur['hdd'], 1, '℃·d')} ℃·d；供热量 {F.n('heat', cur['heat_gj'], 0, 'GJ')} GJ"
              + _chg(F, "heat_wow", pct(cur["heat_gj"], prev.get("heat_gj")), "较上一周期")
              + _chg(F, "heat_yoy", pct(cur["heat_gj"], ly.get("heat_gj")), "较去年同期") + "。")
        p2 = ""
        if cur.get("norm_heat"):
            p2 = (f"度日数修正单耗 {F.n('norm', cur['norm_heat'], 1, 'kJ/(m²·℃·d)')} kJ/(m²·℃·d)"
                  + _chg(F, "norm_wow", norm_change(cur, prev), "较上一周期")
                  + _chg(F, "norm_yoy", norm_change(cur, ly), "较去年同期") + "。")
        trend_x, trend_cur, trend_ly = [], [], []
        dd = start
        while dd < end:
            a = self.summ(scope, dd, dd + timedelta(days=1), sources)
            b = self.summ(scope, dd - YEAR, dd + timedelta(days=1) - YEAR, sources)
            trend_x.append(dd.strftime("%m-%d"))
            trend_cur.append(a.get("norm_heat"))
            trend_ly.append(b.get("norm_heat"))
            dd += timedelta(days=1)
        sections = [{"heading": "一、总体运行", "paragraphs": [x for x in (p1, p2) if x],
                     "chart": {"type": "line", "title": "逐日修正单耗：本期 vs 去年同期（kJ/(m²·℃·d)）", "x": trend_x,
                               "series": [{"name": "本期", "data": trend_cur}, {"name": "去年同期", "data": trend_ly}]}}]
        findings = []
        v = norm_change(cur, ly)
        if v is not None:
            findings.append(f"修正单耗较去年同期{'上升' if v > 0 else '下降'} {F.n('norm_yoy', abs(v), 1)}%。")
        # 站点：达标率与能效
        sts = self.stations_in(scope, sources)
        rows = []
        for sid in sts:
            r = self.summ(sid, start, end)
            if r.get("hours"):
                r["compliance"] = _r((1 - r["tin_low_hours"] / r["hours"]) * 100, 1)
                rows.append(r)
        norms = [r["norm_heat"] for r in rows if r.get("norm_heat")]
        med = float(np.median(norms)) if norms else None
        for r in rows:
            r["vs_median"] = _r((r["norm_heat"] / med - 1) * 100, 1) if med and r.get("norm_heat") else None
        rate, hours, _ = self.compliance(rows)
        paras = []
        if rate is not None:
            paras.append(f"室温达标率 {F.n('compliance', rate * 100, 1, '%')}%（不低于 {F.n('std_tin', STD_TIN, 0, '℃', 'POL-02 §1')}℃ "
                         f"的站时占比，{F.lit('POL-02 §3')}）。")
            findings.append(f"室温达标率 {F.n('compliance', rate * 100, 1)}%。")
            worst_c = sorted(rows, key=lambda r: r["compliance"])[:3]
            worst_c = [r for r in worst_c if r["compliance"] < 100]
            if worst_c:
                paras.append("达标率最低的站：" + "、".join(
                    f"{F.lit(r['name'])} {F.n('comp_' + r['id'], r['compliance'], 1)}%" for r in worst_c) + "。")
        if med and len(rows) >= 3:
            paras.append(f"各站修正单耗中位数 {F.n('median_norm', med, 1, 'kJ/(m²·℃·d)')}，高出中位数最多的站：" + "、".join(
                f"{F.lit(r['name'])}（{'高' if r['vs_median'] >= 0 else '低'} {F.n('vsm_' + r['id'], abs(r['vs_median']), 1)}%）"
                for r in sorted(rows, key=lambda r: -(r["vs_median"] or -999))[:3]) + "。")
        sections.append({"heading": "二、室温达标与能效对标", "paragraphs": paras,
                         "table": {"columns": [["name", "热力站"], ["norm_heat", "修正单耗"], ["vs_median", "较中位数 %"],
                                               ["tin_avg", "平均室温 ℃"], ["compliance", "达标率 %"], ["unit_water", "补水 kg/千m²"]],
                                   "rows": [{k: r.get(k) for k in ("name", "norm_heat", "vs_median", "tin_avg", "compliance", "unit_water")}
                                            for r in sorted(rows, key=lambda r: -(r.get("norm_heat") or 0))]},
                         "chart": {"type": "bar", "title": "各站修正单耗较中位数（%）", "x": [r["name"] for r in rows],
                                   "series": [{"name": "较中位数 %", "data": [r["vs_median"] for r in rows]}]}})
        # 工单与客服
        s_, e_ = _iso(start), _iso(end)
        wos = [w for w in self.ops.workorders.values() if w["station_id"] in sts and s_ <= w["created_at"] < e_]
        closed = [w for w in wos if w["closed_at"]]
        in_time = [w for w in closed if w["closed_at"] <= w["due_at"]]
        complaints = [w for w in wos if w["type"] in ("不热", "过热")]
        area = sum(STATION_BY_ID[x].area for x in sts)
        paras = [f"新建工单 {F.n('wo', len(wos), 0, '张')} 张，其中已完结 {F.n('wo_closed', len(closed), 0, '张')} 张"
                 + (f"，在到场时限内完结 {F.n('wo_intime', len(in_time) / len(closed) * 100, 1, '%')}%" if closed else "") + "。"
                 + "（系统没有到场打卡数据，以完结时间近似，结果偏保守。）"]
        if wos:
            paras.append("类型分布：" + "、".join(f"{t} {F.n('wot_' + t, n, 0)} 张" for t, n in Counter(w["type"] for w in wos).most_common()) + "。")
        paras.append(f"冷热类报修 {F.n('complaints', len(complaints), 0)} 张，折合每万平方米 "
                     f"{F.n('complaint_rate', len(complaints) / (area / 1e4), 2, '张/万m²')} 张。")
        overdue = [w for w in self.ops.workorders.values() if w["station_id"] in sts and w["status"] not in ("已完成", "已关闭")
                   and w["due_at"] < _iso(now_local())]
        if overdue:
            paras.append(f"截至生成时仍有超时未完结工单 {F.n('overdue', len(overdue), 0)} 张。")
            findings.append(f"超时未完结工单 {F.n('overdue', len(overdue), 0)} 张。")
        if ctx["has"]("ops:read"):
            sections.append({"heading": "三、工单与客服", "paragraphs": paras})
        else:
            findings = [f for f in findings if "工单" not in f]
        # 异常检测、报警
        an = [e for e in self.ai.anomalies(7)["data"]["events"] if e["station_id"] in sts] if ctx["has"]("ai:read") else []
        paras = []
        if an:
            paras.append(f"近 {F.n('an_days', 7, 0, '天')} 天（截至生成时）异常检测：" + "；".join(
                f"{F.lit(e['station_name'])} {F.lit(e['type'])}" + ("（已知原因）" if e["known_cause"] else "") for e in an) + "。")
            unknown = [e for e in an if not e["known_cause"]]
            if unknown:
                findings.append("待排查异常：" + "、".join(F.lit(e["station_name"]) + " " + F.lit(e["type"]) for e in unknown) + "。")
        ev_par, ev_find = self._events_paragraphs(F, start, end, scope, sources, ctx["has"])
        sections.append({"heading": "四、异常、报警与调控", "paragraphs": paras + ev_par})
        findings += ev_find
        # 下周天气
        w = weather_report(self.store, 168)["data"]
        fd = w["forecast_daily"]
        if fd:
            p = (f"未来 {F.n('fc_days', len(fd), 0, '天')} 天日最低气温 {F.n('fc_min', min(x['min'] for x in fd), 1, '℃')}℃、"
                 f"日最高 {F.n('fc_max', max(x['max'] for x in fd), 1, '℃')}℃。")
            if w["cold_wave"]:
                h48, h24 = F.n("h48", 48, 0, "h"), F.n("h24", 24, 0, "h")
                p += f"未来 {h48} 小时有寒潮，最低气温较近 {h24} 小时下降 {F.n('drop', w['drop_48h'], 1, '℃')}℃，建议提前预升温。"
                findings.append(f"未来 {h48} 小时有寒潮，需提前预升温。")
            sections.append({"heading": "五、下周天气", "paragraphs": [p]})
        warnings += self.memory_warning(start)
        return {"title": f"{ctx['scope_name']}运行周报（{ctx['label'].replace('~', '至')}）",
                "period": {"start": _iso(start), "end": _iso(end), "label": ctx["label"]}, "sections": sections,
                "key_findings": findings[:6], "warnings": warnings,
                "method": ["环比基准为紧邻的上一同长度周期，同比基准为去年同日历区间；单耗均为度日数修正单耗。",
                           "达标率按站时统计；冷热类报修率 = 不热与过热工单数 ÷ 供热面积（万平方米）。"]}

    # ================================================================ 节能报告
    def _energy(self, ctx) -> dict:
        F, scope, sources, start, end = ctx["F"], ctx["scope"], ctx["sources"], ctx["start"], ctx["end"]
        cur = self.summ(scope, start, end, sources)
        base = self.summ(scope, start - YEAR, end - YEAR, sources)
        exp_h = (min(end, now_local()) - start).total_seconds() / 3600
        if not cur.get("hours") or cur["hours"] < exp_h * 0.9:
            raise ReportError("NO_DATA", "期间内运行数据不完整（本采暖季数据从开栓日起），请缩短期间")
        if not base.get("hours") or not base.get("hdd"):
            raise ReportError("NO_BASELINE", "去年同期无基准数据")
        A = area_of(scope, sources)
        Qc, Qb, Hc, Hb = cur["heat_gj"], base["heat_gj"], cur["hdd"], base["hdd"]
        Ic, Ib = Qc * 1e6 / (A * Hc), Qb * 1e6 / (A * Hb)             # kJ/(m²·℃·d)
        expected = Qb * Hc / Hb                                       # 按去年单耗、今年天气应耗热量
        weather_eff = Qb * (Hc - Hb) / Hb
        intensity_eff = Qc - expected
        saved = expected - Qc
        rate = saved / expected * 100
        tce = saved * GJ_TO_KGCE / 1000
        tco2 = tce * KGCE_TO_KGCO2
        name = F.lit(ctx["scope_name"])
        lab = F.lit(ctx["label"].replace("~", "至"))
        p1 = (f"{lab}，{name}供热面积 {F.n('area', A / 1e4, 1, '万m²')} 万m²。本期室外平均气温 {F.n('tout', cur['tout_avg'], 1, '℃')}℃、"
              f"度日数 {F.n('hdd', Hc, 1, '℃·d')} ℃·d，供热量 {F.n('heat', Qc, 0, 'GJ')} GJ；去年同期室外平均气温 "
              f"{F.n('tout_base', base['tout_avg'], 1, '℃')}℃、度日数 {F.n('hdd_base', Hb, 1, '℃·d')} ℃·d，供热量 {F.n('heat_base', Qb, 0, 'GJ')} GJ。")
        p2 = (f"热量变化 {F.n('dq', Qc - Qb, 0, 'GJ')} GJ 可分解为两部分：天气因素 {F.n('weather_eff', weather_eff, 0, 'GJ')} GJ"
              f"（按去年单耗，因度日数变化应增减的热量），运行因素 {F.n('intensity_eff', intensity_eff, 0, 'GJ')} GJ"
              "（单耗变化带来的部分，负值表示节约）。两项之和等于总变化。")
        if saved >= 0:
            p3 = (f"度日数修正单耗由 {F.n('norm_base', Ib, 1, 'kJ/(m²·℃·d)')} 降至 {F.n('norm', Ic, 1, 'kJ/(m²·℃·d)')} kJ/(m²·℃·d)，"
                  f"节能率 {F.n('rate', rate, 1, '%')}%；在本期天气条件下比按去年单耗运行少耗热 {F.n('saved', saved, 0, 'GJ')} GJ，"
                  f"折合标煤 {F.n('tce', tce, 1, 't')} t、减排 CO₂ {F.n('tco2', tco2, 1, 't')} t。")
        else:
            p3 = (f"度日数修正单耗由 {F.n('norm_base', Ib, 1, 'kJ/(m²·℃·d)')} 升至 {F.n('norm', Ic, 1, 'kJ/(m²·℃·d)')} kJ/(m²·℃·d)，"
                  f"在本期天气条件下比按去年单耗运行多耗热 {F.n('saved', -saved, 0, 'GJ')} GJ（{F.n('rate', -rate, 1, '%')}%），"
                  f"折合标煤 {F.n('tce', -tce, 1, 't')} t。")
        findings = [(f"节能率 {F.n('rate', rate, 1)}%，节约 {F.n('saved', saved, 0)} GJ，折合标煤 {F.n('tce', tce, 1)} t。" if saved >= 0 else
                     f"修正单耗同比上升 {F.n('rate', -rate, 1)}%，多耗热 {F.n('saved', -saved, 0)} GJ。"),
                    f"热量变化中天气因素 {F.n('weather_eff', weather_eff, 0)} GJ、运行因素 {F.n('intensity_eff', intensity_eff, 0)} GJ。"]
        # 逐日趋势
        tx, tc_, tb_ = [], [], []
        dd = start
        while dd < end:
            a = self.summ(scope, dd, dd + timedelta(days=1), sources)
            b = self.summ(scope, dd - YEAR, dd + timedelta(days=1) - YEAR, sources)
            tx.append(dd.strftime("%m-%d"))
            tc_.append(a.get("norm_heat"))
            tb_.append(b.get("norm_heat"))
            dd += timedelta(days=1)
        sections = [{"heading": "一、总体节能效果", "paragraphs": [p1, p2, p3],
                     "chart": {"type": "line", "title": "逐日修正单耗：本期 vs 去年同期（kJ/(m²·℃·d)）", "x": tx,
                               "series": [{"name": "本期", "data": tc_}, {"name": "去年同期", "data": tb_}]},
                     "table": {"columns": [["item", "项目"], ["cur", "本期"], ["base", "去年同期"]],
                               "rows": [{"item": "室外平均气温 ℃", "cur": cur["tout_avg"], "base": base["tout_avg"]},
                                        {"item": "度日数 ℃·d", "cur": _r(Hc, 1), "base": _r(Hb, 1)},
                                        {"item": "供热量 GJ", "cur": _r(Qc, 0), "base": _r(Qb, 0)},
                                        {"item": "修正单耗 kJ/(m²·℃·d)", "cur": _r(Ic, 1), "base": _r(Ib, 1)},
                                        {"item": "平均室温 ℃", "cur": cur.get("tin_avg"), "base": base.get("tin_avg")}]}}]
        # 站点对标
        sts = set(self.stations_in(scope, sources))
        rows = [r for r in self.analytics.energy(start, end, sources)["stations"] if r["id"] in sts]
        paras = []
        good = sorted([r for r in rows if r.get("saving_rate") is not None], key=lambda r: -r["saving_rate"])
        if len(good) >= 2:
            paras.append("节能率最高：" + "、".join(f"{F.lit(r['name'])} {F.n('sr_' + r['id'], r['saving_rate'], 1)}%" for r in good[:3]) + "。")
            bad = [r for r in good[::-1] if r["saving_rate"] < 0][:3]
            if bad:
                paras.append("单耗同比上升：" + "、".join(f"{F.lit(r['name'])}（上升 {F.n('sr_' + r['id'], -r['saving_rate'], 1)}%）" for r in bad) + "。")
                findings.append("单耗同比上升的站：" + "、".join(F.lit(r["name"]) for r in bad) + "。")
        over = [r for r in rows if (r.get("overheat_gj") or 0) > 0]
        if over:
            tot = sum(r["overheat_gj"] for r in over)
            paras.append(f"过热挖潜：{F.n('n_over', len(over), 0)} 个站平均室温高于 {F.n('t_target', T_ROOM_TARGET, 0, '℃', '口径常数')}℃，"
                         f"降至该温度估计可少供热 {F.n('overheat_total', tot, 0, 'GJ')} GJ，最大的是 " + "、".join(
                             f"{F.lit(r['name'])}（{F.n('oh_' + r['id'], r['overheat_gj'], 0)} GJ）"
                             for r in sorted(over, key=lambda r: -r["overheat_gj"])[:3]) + "。")
            findings.append(f"过热挖潜约 {F.n('overheat_total', tot, 0)} GJ。")
        sections.append({"heading": "二、站点对标", "paragraphs": paras,
                         "table": {"columns": [["name", "热力站"], ["norm_heat", "修正单耗"], ["base_norm_heat", "去年同期"],
                                               ["saving_rate", "节能率 %"], ["saved_gj", "节约 GJ"], ["vs_median", "较中位数 %"],
                                               ["tin_avg", "平均室温 ℃"], ["overheat_gj", "过热挖潜 GJ"]],
                                   "rows": [{k: r.get(k) for k in ("name", "norm_heat", "base_norm_heat", "saving_rate", "saved_gj",
                                                                   "vs_median", "tin_avg", "overheat_gj")} for r in rows]},
                         "chart": {"type": "bar", "title": "各站节能率（%）", "x": [r["name"] for r in rows],
                                   "series": [{"name": "节能率 %", "data": [r.get("saving_rate") for r in rows]}]}})
        # 措施效果（DiD）
        paras = []
        for sid in sorted(s for s in ADOPTIONS if s in sts):
            try:
                e = self.ai.control_effect(sid)["data"]
            except ValueError:
                continue
            paras.append(f"{F.lit(e['station_name'])}于 {F.lit(e['switch'])} 采用优化曲线：本站修正单耗变化 "
                         f"{F.n('did_t_' + sid, e['treat']['change_pct'], 1, '%')}%，对照组（{F.n('did_nc_' + sid, len(e['control']['ids']), 0)} 个未调整站）"
                         f"变化 {F.n('did_c_' + sid, e['control']['change_pct'], 1, '%')}%，净效果 {F.n('did_' + sid, e['did_pct'], 1, '%')}%，"
                         f"室温净变化 {F.n('did_tin_' + sid, e['tin_did'], 2, '℃')}℃。")
        if paras:
            paras.append("双重差分用同期未调整的站作对照，扣除天气和全网共同变化；前提是两组在调整前趋势平行，"
                         "且对照站同期没有其他措施。窗口较短时结论不稳定。")
            sections.append({"heading": "三、措施效果评估（双重差分）", "paragraphs": paras})
        cav = ["基准为去年同日历区间；如去年同期有停运、检修或用户报停比例变化，基准会失真。",
               "度日数修正假设耗热量与度日数成正比，忽略太阳辐射、风速和建筑蓄热；极冷或极暖天偏差会增大。",
               "供热面积按台账常数计，未扣除报停面积；总体节能量按本范围总供热量计算，站点节能量之和不含管网损失，两者不相等。",
               "本系统运行数据为仿真数据，报告用于演示方法。"]
        sections.append({"heading": "四、口径与局限", "paragraphs": cav})
        return {"title": f"{ctx['scope_name']}节能分析报告（{ctx['label'].replace('~', '至')}）",
                "period": {"start": _iso(start), "end": _iso(end), "label": ctx["label"]}, "sections": sections,
                "key_findings": findings[:5], "warnings": [],
                "method": [f"度日数 HDD 基准温度 {F.n('t_balance', T_BALANCE, 0, '℃', '口径常数')}℃；修正单耗 = 热量 ÷（面积 × HDD）。",
                           "应耗热量 = 去年热量 × 本期HDD ÷ 去年HDD；节能量 = 应耗热量 − 实际热量；节能率 = 节能量 ÷ 应耗热量。",
                           "天气因素 = 去年热量 ×（本期HDD − 去年HDD）÷ 去年HDD；运行因素 = 实际热量 − 应耗热量。",
                           f"标煤折算 {F.n('gj_kgce', GJ_TO_KGCE, 2, 'kgce/GJ', '口径常数')} kgce/GJ，CO₂ 排放因子 "
                           f"{F.n('kgce_co2', KGCE_TO_KGCO2, 2, 'kgCO₂/kgce', '口径常数')} kgCO₂/kgce。",
                           f"过热挖潜 ≈（平均室温 − {F.n('t_target', T_ROOM_TARGET, 0)}）÷（平均室温 − 室外均温）× 供热量。"]}

    # ================================================================ 事故报告
    def _incident(self, ctx, event_id: str) -> dict:
        F, sources, can_see = ctx["F"], ctx["sources"], ctx["can_see"]
        eid = event_id.strip()
        now = now_local()
        alarm = next((a for a in self.engine.alarm_log if a["id"] == eid), None)
        cmd = self.control.commands.get(eid.upper()) if not alarm else None
        if alarm:
            oid, oname = alarm["object_id"], alarm["object_name"]
            t0 = datetime.fromisoformat(alarm["first_ts"])
            t1 = datetime.fromisoformat(alarm["cleared_ts"]) if alarm.get("cleared_ts") else now
            desc, codes = alarm["message"], [alarm["code"]]
            kind = f"{alarm['level']}级报警"
        elif cmd:
            oid, oname = cmd["station_id"], cmd["station_name"]
            t0 = datetime.fromisoformat(cmd["executed_at"])
            t1 = datetime.fromisoformat(cmd["rollback"]["ts"]) if cmd.get("rollback") else now
            desc, codes = f"调控指令 {cmd['command_id']}：{cmd['description']}", []
            kind = "调控指令" + ("（已回滚）" if cmd["status"] == "rolled_back" else "")
        else:
            raise ReportError("NOT_FOUND", f"事件 {eid} 不存在（支持报警 ID 或指令号；报警记录自系统启动起保存）")
        if not can_see(oid):
            raise ReportError("OUT_OF_SCOPE", "该事件不在数据范围内")
        net = STATION_BY_ID[oid].source_id if oid in STATION_BY_ID else oid
        ws, we = t0 - timedelta(hours=2), t1 + timedelta(hours=1)
        s, e = _iso(ws), _iso(we)
        objs = {oid, net}
        timeline = []
        for a in reversed(self.engine.alarm_log):
            if a["object_id"] in objs and s <= a["first_ts"] < e:
                timeline.append({"ts": a["first_ts"], "kind": "报警", "actor": "系统",
                                 "text": f"{a['level']}级 {a['object_name']}：{a['message']}"})
                if a.get("cleared_ts") and a["cleared_ts"] < e:
                    timeline.append({"ts": a["cleared_ts"], "kind": "报警恢复", "actor": "系统", "text": f"{a['object_name']}：{a['message']}"})
        act = {"draft": "草拟指令", "check": "安全校验", "request_approval": "提交审批", "approve": "批准", "reject": "驳回",
               "execute": "下发", "rollback": "回滚", "breaker_trip": "熔断", "breaker_reset": "解除熔断", "tool_call": "智能体工具调用",
               "auto_sign": "自动签发令牌", "approval_expired": "审批过期", "workorder_create": "创建工单", "workorder_assign": "派单",
               "workorder_status": "工单状态", "handoff_create": "客服移交", "notice_send": "发送通知", "alarm_ack": "确认报警"}
        for a in reversed(self.control.audit):            # 审计按新→旧存储；倒序遍历使同一秒内的记录保持发生顺序
            if a["target"] in objs and s <= a["ts"] < e and a["action"] in act:
                d = a["detail"]
                text = (d.get("tool") and f"{d['tool']}：{d.get('summary', '')}") or d.get("desc") or d.get("reason") or \
                    "；".join(d.get("reasons", [])) or d.get("workorder_id") or d.get("draft_id") or d.get("command_id") or ""
                w = self.ops.workorders.get(d.get("workorder_id") or "")
                if w:
                    text = f"{w['workorder_id']} {w['type']}：{w['description']}"
                timeline.append({"ts": a["ts"], "kind": act[a["action"]], "actor": a["user"], "text": f"{text}（{a['result']}）"})
        for w in self.ops.workorders.values():
            if w["station_id"] in objs and s <= w["created_at"] < e and not any(
                    t["kind"] == "创建工单" and w["workorder_id"] in t["text"] for t in timeline):
                timeline.append({"ts": w["created_at"], "kind": "工单", "actor": w["created_by"],
                                 "text": f"{w['workorder_id']} {w['type']}：{w['description']}"})
        timeline.sort(key=lambda x: x["ts"])
        cnt = Counter(t["kind"] for t in timeline)
        tool_calls = [t for t in timeline if t["kind"] == "智能体工具调用"]
        F.lit(oname)
        dur_h = (t1 - t0).total_seconds() / 3600
        dur_txt = (f"{F.n('duration_h', dur_h, 1, 'h')} 小时" if dur_h >= 1 else
                   f"{F.n('duration_min', dur_h * 60, 0, 'min')} 分钟")
        p1 = (f"事件：{F.lit(kind)}，对象 {F.lit(oname)}，内容「{F.lit(desc)}」。发生于 {F.lit(t0.strftime('%m-%d %H:%M'))}，"
              + (f"于 {F.lit(t1.strftime('%m-%d %H:%M'))} 恢复/结束" if t1 < now else "截至报告生成时尚未恢复")
              + f"，持续约 {dur_txt}。")
        p2 = (f"复盘窗口（事件前 {F.n('pre_h', 2, 0, 'h')} 小时至结束后 {F.n('post_h', 1, 0, 'h')} 小时）内共有 "
              f"{F.n('timeline', len(timeline), 0)} 条记录：报警 {F.n('tl_alarm', cnt.get('报警', 0), 0)} 条，"
              f"调控相关操作 {F.n('tl_ctrl', sum(cnt.get(k, 0) for k in ('草拟指令', '安全校验', '提交审批', '批准', '驳回', '下发', '回滚', '自动签发令牌')), 0)} 条"
              f"（其中下发 {F.n('tl_exec', cnt.get('下发', 0), 0)}、回滚 {F.n('tl_rb', cnt.get('回滚', 0), 0)}），"
              f"智能体工具调用 {F.n('tl_tool', len(tool_calls), 0)} 次，熔断 {F.n('tl_trip', cnt.get('熔断', 0), 0)} 次，"
              f"工单 {F.n('tl_wo', cnt.get('创建工单', 0) + cnt.get('工单', 0), 0)} 张。")
        agents = sorted({t["actor"] for t in tool_calls})
        p3 = ("参与的智能体：" + "、".join(F.lit(a) for a in agents) + "。智能体的每次工具调用均有审计记录，可按 trace_id 回放。") if agents \
            else "窗口内没有智能体参与，均为人工或系统操作。"
        sections = [{"heading": "一、事件概述", "paragraphs": [p1]},
                     {"heading": "二、处置时间线", "paragraphs": [p2, p3],
                      "table": {"columns": [["ts", "时间"], ["kind", "类型"], ["actor", "操作人"], ["text", "内容"]],
                                "rows": [{**t, "ts": t["ts"][5:16].replace("T", " ")} for t in timeline]}}]
        # 运行数据对比
        df = self.store.frame(oid, sources)
        pre = df[(df.index >= t0 - timedelta(hours=6)) & (df.index < t0)]
        post = df[(df.index >= t0) & (df.index < max(t1, t0 + timedelta(hours=1)))]
        metrics = [("tin", "典型室温", "℃", 2), ("ts2", "二次供温", "℃", 1), ("valve", "阀门开度", "%", 1),
                   ("water_t", "补水量", "t/h", 2), ("heat_mw", "热负荷", "MW", 2), ("ts1", "一次供温", "℃", 1)]
        rows, paras = [], []
        for col, label, unit, nd in metrics:
            if col in df.columns and len(pre) and len(post):
                a, b = float(pre[col].mean()), float(post[col].mean())
                rows.append({"metric": f"{label} {unit}", "pre": _r(a, nd), "post": _r(b, nd), "delta": _r(b - a, nd)})
        if rows:
            paras.append(f"对比事件前 {F.n('pre_win', 6, 0, 'h')} 小时与事件期间的小时均值（逐时数据，事件不足 {F.n('one_h', 1, 0, 'h')} 小时按 {F.n('one_h', 1, 0, 'h')} 小时取）：" + "；".join(
                f"{F.lit(r['metric'].split(' ')[0])} {F.n('pre_' + r['metric'], r['pre'], 2)}→{F.n('post_' + r['metric'], r['post'], 2)}"
                for r in rows[:4]) + "。")
        else:
            paras.append("事件窗口内逐时数据不足，未做前后对比。")
        chart = None
        win = df[(df.index >= ws - timedelta(hours=4)) & (df.index <= we)]
        if len(win) >= 3:
            ser = [{"name": f"{lbl} {u}", "data": [_r(v, nd) for v in win[c].tolist()], **({"axis": "right"} if i else {})}
                   for i, (c, lbl, u, nd) in enumerate(metrics[:2]) if c in win.columns] \
                or [{"name": "热负荷 MW", "data": [_r(v, 2) for v in win["heat_mw"].tolist()]}]
            chart = {"type": "line", "title": "事件前后逐时数据", "x": [t.strftime("%m-%d %H:%M") for t in win.index], "series": ser}
        sections.append({"heading": "三、运行数据", "paragraphs": paras, "chart": chart,
                         "table": {"columns": [["metric", "指标"], ["pre", "事件前"], ["post", "事件期间"], ["delta", "变化"]], "rows": rows}})
        # 相似案例与规程
        otype = "station" if oid in STATION_BY_ID else "source"
        cases = self.kb.similar_cases(desc, object_type=otype, alarm_codes=codes, top_k=3)["cases"]
        docs = self.kb.search(desc, top_k=3)["results"]
        paras = []
        if cases:
            paras.append("相似历史案例（仅供参考，原因未经本次确认）：" + "；".join(
                f"{F.lit(c['case_id'])}「{F.lit(c['title'])}」，当时原因：{F.lit(c.get('cause', '未记录'))}" for c in cases) + "。")
        else:
            paras.append("知识库中没有足够相似的已审核案例。")
        if docs:
            paras.append("相关规程：" + "；".join(f"{F.lit(d['doc_id'])} {F.lit(d['section'])}" for d in docs) + "。")
        sections.append({"heading": "四、相似案例与规程依据", "paragraphs": paras})
        sections.append({"heading": "五、待人工确认", "paragraphs": [
            "本报告由系统按审计与运行记录自动整理，只陈述发生了什么；原因判断、责任认定和整改措施须由值班调度长审核后填写。"
            "审核通过后结论附在报告末尾。"]})
        findings = [f"{F.lit(oname)}「{F.lit(desc)}」持续约 {dur_txt}。",
                    f"窗口内下发 {F.n('tl_exec', cnt.get('下发', 0), 0)} 次、回滚 {F.n('tl_rb', cnt.get('回滚', 0), 0)} 次、"
                    f"智能体工具调用 {F.n('tl_tool', len(tool_calls), 0)} 次。"]
        if cases:
            findings.append(f"最相似案例 {F.lit(cases[0]['case_id'])}「{F.lit(cases[0]['title'])}」（待人工确认是否同因）。")
        return {"title": f"事故报告：{oname} {kind}（{t0:%m-%d %H:%M}）", "scope_id": oid, "scope_name": oname,
                "event_id": eid, "status": "草稿（待审核）",
                "period": {"start": s, "end": e, "label": eid}, "sections": sections, "key_findings": findings,
                "warnings": self.memory_warning(ws), "method": ["时间线来自报警记录、审计日志（含智能体工具调用）与工单；运行数据为逐时均值。"]}

    # ================================================================ HTML
    def render_html(self, rep: dict) -> str:
        e = html.escape
        parts = [f"<h1>{e(rep['title'])}</h1>",
                 f"<div class='meta'>{e(rep['type_name'])} · 范围 {e(rep['scope_name'])} · 期间 {e(rep['period']['label'])} · "
                 f"生成 {e(rep['generated_at'].replace('T', ' '))} · {e(rep['generated_by'])} · 状态 {e(rep['status'])}</div>"]
        ck = rep["check"]
        parts.append(f"<div class='check {'ok' if ck['ok'] else 'bad'}'>数字核对：{'通过' if ck['ok'] else '未通过'}，"
                     f"正文 {ck['numbers_checked']} 个数字均可追溯到 {ck['facts_registered']} 项登记数据 · 数据指纹 {e(rep['fingerprint'])}</div>")
        for w in rep.get("warnings", []):
            parts.append(f"<div class='warn'>⚠ {e(w)}</div>")
        if rep["key_findings"]:
            parts.append("<h2>关键结论</h2><ul class='find'>" + "".join(f"<li>{e(x)}</li>" for x in rep["key_findings"]) + "</ul>")
        for s in rep["sections"]:
            parts.append(f"<h2>{e(s['heading'])}</h2>")
            if s.get("human_written"):
                parts.append("<div class='human'>以下为人工填写内容</div>")
            parts += [f"<p>{e(p)}</p>" for p in s.get("paragraphs", [])]
            if s.get("chart"):
                parts.append(svg_chart(s["chart"]))
            t = s.get("table")
            if t and t["rows"]:
                head = "".join(f"<th>{e(lbl)}</th>" for _, lbl in t["columns"])
                body = "".join("<tr>" + "".join(f"<td>{e(_cell(r.get(k)))}</td>" for k, _ in t["columns"]) + "</tr>" for r in t["rows"])
                parts.append(f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>")
        if rep.get("method"):
            parts.append("<h2>口径说明</h2><ul class='method'>" + "".join(f"<li>{e(m)}</li>" for m in rep["method"]) + "</ul>")
        return ("<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
                f"<title>{e(rep['title'])}</title><style>{CSS}</style></head><body><main>" + "".join(parts) + "</main></body></html>")


def _cell(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:g}" if abs(v) >= 1e-3 or v == 0 else f"{v:.4f}"
    return str(v)


COLORS = ["#2563eb", "#ea580c", "#16a34a", "#9333ea"]


def svg_chart(c: dict, w: int = 760, h: int = 260) -> str:
    """服务端生成的简单 SVG 图表（折线/柱状），用于自包含 HTML 与打印。"""
    e = html.escape
    x = c.get("x", [])
    series = [s for s in c.get("series", []) if any(v is not None for v in s["data"])]
    if not x or not series:
        return ""
    L, R, T, B = 56, 56, 46, 40
    pw, ph = w - L - R, h - T - B

    def scale(vals):
        vs = [v for v in vals if v is not None]
        lo, hi = min(vs), max(vs)
        if c["type"] == "bar":
            lo, hi = min(lo, 0), max(hi, 0)
        if hi == lo:
            hi, lo = hi + 1, lo - 1
        pad = (hi - lo) * 0.08
        return lo - (0 if c["type"] == "bar" and lo == 0 else pad), hi + pad

    left = [s for s in series if s.get("axis") != "right"]
    right = [s for s in series if s.get("axis") == "right"]
    ly = scale([v for s in left for v in s["data"]]) if left else None
    ry = scale([v for s in right for v in s["data"]]) if right else None
    out = [f"<svg viewBox='0 0 {w} {h}' class='chart' role='img' aria-label='{e(c.get('title', ''))}'>",
           f"<text x='{L}' y='16' class='ct'>{e(c.get('title', ''))}</text>"]
    for i in range(5):
        yy = T + ph * i / 4
        out.append(f"<line x1='{L}' x2='{L + pw}' y1='{yy:.1f}' y2='{yy:.1f}' class='grid'/>")
        if ly:
            out.append(f"<text x='{L - 6}' y='{yy + 4:.1f}' text-anchor='end' class='ax'>{_tick(ly[1] - (ly[1] - ly[0]) * i / 4)}</text>")
        if ry:
            out.append(f"<text x='{L + pw + 6}' y='{yy + 4:.1f}' class='ax'>{_tick(ry[1] - (ry[1] - ry[0]) * i / 4)}</text>")
    n = len(x)
    step = max(1, n // 8)
    for i in range(0, n, step):
        xx = L + (pw * (i + 0.5) / n if c["type"] == "bar" else pw * i / max(n - 1, 1))
        out.append(f"<text x='{xx:.1f}' y='{h - B + 16}' text-anchor='middle' class='ax'>{e(str(x[i])[:8])}</text>")
    for k, s in enumerate(series):
        lo, hi = (ry if s.get("axis") == "right" else ly)
        col = COLORS[k % len(COLORS)]
        fy = lambda v: T + ph * (1 - (v - lo) / (hi - lo))  # noqa: E731
        if c["type"] == "bar":
            bw = pw / n * 0.7 / len(series)
            for i, v in enumerate(s["data"]):
                if v is None:
                    continue
                x0 = L + pw * i / n + pw / n * 0.15 + k * bw
                y0, y1 = fy(max(v, 0)), fy(min(v, 0))
                out.append(f"<rect x='{x0:.1f}' y='{y0:.1f}' width='{bw:.1f}' height='{max(y1 - y0, 0.5):.1f}' fill='{col}'/>")
        else:
            pts, segs = [], []
            for i, v in enumerate(s["data"]):
                if v is None:
                    if pts:
                        segs.append(pts)
                    pts = []
                    continue
                pts.append(f"{L + pw * i / max(n - 1, 1):.1f},{fy(v):.1f}")
            if pts:
                segs.append(pts)
            for pp in segs:
                out.append(f"<polyline points='{' '.join(pp)}' fill='none' stroke='{col}' stroke-width='2'/>")
        out.append(f"<rect x='{L + k * 150}' y='28' width='12' height='4' fill='{col}'/>"
                   f"<text x='{L + 16 + k * 150}' y='34' class='ax'>{e(s['name'])}</text>")
    out.append("</svg>")
    return "".join(out)


def _tick(v: float) -> str:
    return f"{v:.0f}" if abs(v) >= 100 else f"{v:.1f}"


CSS = """
body{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;color:#1f2937;background:#f3f4f6;margin:0}
main{max-width:860px;margin:24px auto;background:#fff;padding:32px 40px;box-shadow:0 1px 3px rgba(0,0,0,.1)}
h1{font-size:22px;margin:0 0 6px}h2{font-size:16px;margin:24px 0 8px;border-left:4px solid #2563eb;padding-left:8px}
p,li{font-size:14px;line-height:1.85}.meta{font-size:12px;color:#6b7280}
.check{font-size:12px;margin:10px 0;padding:6px 10px;border-radius:4px}.check.ok{background:#ecfdf5;color:#047857}
.check.bad{background:#fef2f2;color:#b91c1c}.warn{font-size:12px;color:#92400e;background:#fffbeb;padding:6px 10px;margin:4px 0;border-radius:4px}
.find li{font-weight:600}.human{font-size:12px;color:#7c3aed}
table{border-collapse:collapse;width:100%;font-size:12px;margin:8px 0}th,td{border:1px solid #e5e7eb;padding:4px 6px;text-align:left}
th{background:#f9fafb}.chart{width:100%;height:auto;margin:6px 0}.chart .grid{stroke:#e5e7eb}.chart .ax{font-size:10px;fill:#6b7280}
.chart .ct{font-size:12px;fill:#374151;font-weight:600}.method li{font-size:12px;color:#4b5563}
@media print{body{background:#fff}main{box-shadow:none;margin:0;max-width:none}}
@media (max-width:600px){main{padding:16px}}
"""
