"""可视化（设计文档第八节 viz_render，R0）：生成图表，或在管网拓扑（map）/ 工艺图（twin）中高亮定位。

“生成式界面”的原则：模型只给出图表规格，数据由平台凭 data_ref 取回，模型不搬运数据。
  · data_ref 只接受白名单内的几种数据集（其他工具返回的 data_ref 形式），不是任意 URL——
    否则智能体可以让查看者的浏览器去请求任意接口（例如带副作用或含个人信息的接口）；
  · data_ref 每次渲染时用**查看者**的身份和数据范围在服务端解析，范围外的对象拿不到；
  · 图表规格是受限结构（类型、序列、参考线），不接受原始 ECharts option——原始 option 可以内联任意数据，
    tooltip formatter 还会把字符串当 HTML 渲染；
  · 渲染结果保存为可视化记录，属于委托人本人，可固定到“工作台”，固定后每次打开都用最新数据重绘。
"""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ..config import METRICS, SOURCE_BY_ID, STATION_BY_ID, STATIONS
from ..sim.history import now_local
from .weather_svc import weather_report

CHART_TYPES = ("line", "bar", "area", "scatter")
COLOR_BY = {"alarm": ("报警状态", ""), "tin": ("典型室温", "℃"), "heat_mw": ("热功率", "MW"), "ts2": ("二次供温", "℃"),
            "valve": ("阀门开度", "%"), "makeup_tph": ("补水量", "t/h")}
STATION_PARTS = {"valve": "电动调节阀", "hx": "板式换热器", "heat_meter": "热量表", "pump": "循环泵",
                 "makeup": "补水系统", "building": "用户楼栋"}
SOURCE_PARTS = {"energy": "能源输入", "units": "热源机组", "pumps": "循环泵", "makeup": "补水系统"}
LEVELS = ("info", "warn", "danger")
REALTIME_FIELDS = {"tin": ("典型室温", "℃"), "ts2": ("二次供水温度", "℃"), "tr2": ("二次回水温度", "℃"),
                   "heat_mw": ("热功率", "MW"), "valve": ("调节阀开度", "%"), "g2": ("二次流量", "t/h"),
                   "makeup_tph": ("补水量", "t/h"), "ts1": ("一次供水温度", "℃"), "tr1": ("一次回水温度", "℃")}
MAX_SERIES, MAX_MARKS, MAX_OBJECTS, KEEP_UNPINNED = 8, 4, 20, 100
DATA_REF_FORMS = [
    "/api/history/{对象ID}?hours=1-1440&metrics=指标1,指标2   逐时历史（data_get_timeseries_summary 的 data_ref）",
    "/api/ai/forecast?scope={NET|热源|站}&horizon=24|72|168   负荷预测（实际、P10/P50/P90）",
    "/api/weather?hours=6-168   逐时气温预报",
    "/api/analytics/energy?days=1-60[&view=stations]   修正单耗逐日趋势，或各站对标（view=stations）",
    "/api/ai/effect/{站ID}[?switch=YYYY-MM-DDTHH:MM]   调控效果逐日序列（处理站 vs 对照组）",
    "/api/realtime   各站实时值（按站分类）",
]


class VizError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _text(v, n: int) -> str:
    """标题、名称、注释只作纯文本：去掉尖括号和控制字符，限长。"""
    s = re.sub(r"[<>\x00-\x1f]", "", str(v or "")).strip()
    return s[:n]


def _only(d: dict, allowed: set[str], what: str):
    extra = set(d) - allowed
    if extra:
        raise VizError("BAD_SPEC", f"{what}不支持字段：{'、'.join(sorted(extra))}（只接受 {'、'.join(sorted(allowed))}）")


class Viewer:
    """解析 data_ref 时所用的身份：查看者的数据范围与权限。"""

    def __init__(self, sources, can_see, has, snapshot):
        self.sources, self.can_see, self.has, self.snapshot = sources, can_see, has, snapshot

    def need(self, perm: str):
        if not self.has(perm):
            raise VizError("FORBIDDEN", f"没有查看该数据集的权限（{perm}）")

    def see(self, oid: str) -> str:
        oid = oid.strip().upper()
        if oid not in STATION_BY_ID and oid not in SOURCE_BY_ID and oid != "NET":
            raise VizError("BAD_REF", f"对象 {oid} 不存在")
        if not self.can_see(oid):
            raise VizError("OUT_OF_SCOPE", f"{oid} 不在数据范围内")
        return oid


class VizService:
    def __init__(self, store, ai, analytics, data_dir=None):
        self.store, self.ai, self.analytics = store, ai, analytics
        base = Path(data_dir or os.environ.get("HEATNET_DATA_DIR") or Path(__file__).resolve().parents[2] / "data")
        self.path = base / "viz.json"
        self.lock = threading.RLock()
        self.items: dict[str, dict] = {}
        if self.path.exists():
            try:
                self.items = {v["viz_id"]: v for v in json.loads(self.path.read_text(encoding="utf-8"))}
            except (ValueError, KeyError):
                self.items = {}

    def _persist(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(list(self.items.values()), ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    # ================================================================ 数据集
    def resolve(self, ref: str, v: Viewer) -> dict:
        """data_ref → {kind: time|category, x: [...], fields: {name: {label, unit, values}}, title}。"""
        if not isinstance(ref, str) or len(ref) > 300:
            raise VizError("BAD_REF", "data_ref 须为字符串")
        u = urlsplit(ref.strip())
        if u.scheme or u.netloc:
            raise VizError("BAD_REF", "data_ref 只能是平台内的数据集路径，不能是外部地址")
        q = {k: vs[-1] for k, vs in parse_qs(u.query).items()}
        path = u.path.rstrip("/")

        def num(key, lo, hi, default):
            try:
                x = int(q.get(key, default))
            except ValueError:
                raise VizError("BAD_REF", f"{key} 须为整数") from None
            if not lo <= x <= hi:
                raise VizError("BAD_REF", f"{key} 须在 {lo}–{hi} 之间")
            return x

        m = re.fullmatch(r"/api/history/([A-Za-z0-9]+)", path)
        if m:
            v.need("monitor:read")
            oid = v.see(m.group(1))
            df = self.store.frame(oid, v.sources).iloc[-num("hours", 1, 1440, 48):]
            mets = [x for x in (q.get("metrics") or "").split(",") if x]
            bad = [x for x in mets if x not in METRICS or x not in df.columns]
            if bad:
                raise VizError("BAD_REF", f"{oid} 没有指标 {'、'.join(bad)}；可用：{'、'.join(c for c in METRICS if c in df.columns)}")
            mets = mets or [c for c in ("heat_mw", "tin", "tout") if c in df.columns]
            return {"kind": "time", "title": _name(oid), "x": [t.strftime("%m-%d %H:%M") for t in df.index],
                    "fields": {c: {"label": METRICS[c][0], "unit": METRICS[c][1],
                                   "values": [_num(x) for x in df[c].tolist()]} for c in mets}}
        if path == "/api/ai/forecast":
            v.need("ai:read")
            scope = v.see(q.get("scope", "NET"))
            h = num("horizon", 24, 168, 72)
            d = self.ai.forecast(scope, h, v.sources)["data"]
            hx, fx = d["history"]["ts"], d["forecast"]["ts"]
            pad_h, pad_f = [None] * len(hx), [None] * len(fx)
            return {"kind": "time", "title": f"{d['scope_name']}负荷预测", "x": hx + fx,
                    "fields": {"actual": {"label": "实际负荷", "unit": "MW", "values": d["history"]["heat_mw"] + pad_f},
                               "p50": {"label": "预测 P50", "unit": "MW", "values": pad_h + d["forecast"]["p50"]},
                               "p10": {"label": "预测 P10", "unit": "MW", "values": pad_h + d["forecast"]["p10"]},
                               "p90": {"label": "预测 P90", "unit": "MW", "values": pad_h + d["forecast"]["p90"]},
                               "tout": {"label": "室外温度", "unit": "℃", "values": d["history"]["tout"] + d["forecast"]["tout"]}}}
        if path == "/api/weather":
            v.need("monitor:read")
            w = weather_report(self.store, num("hours", 6, 168, 72))["data"]["forecast_hourly"]
            return {"kind": "time", "title": "气温预报", "x": [x["ts"] for x in w],
                    "fields": {"tout": {"label": "室外温度", "unit": "℃", "values": [x["tout"] for x in w]}}}
        if path == "/api/analytics/energy":
            v.need("analytics:read")
            days = num("days", 1, 60, 7)
            end = now_local()
            e = self.analytics.energy(end - timedelta(days=days), end, v.sources)
            if q.get("view") == "stations":
                rows = e["stations"]
                f = {"norm_heat": ("修正单耗", "kJ/(m²·℃·d)"), "base_norm_heat": ("去年同期修正单耗", "kJ/(m²·℃·d)"),
                     "saving_rate": ("节能率", "%"), "vs_median": ("较中位数", "%"), "tin_avg": ("平均室温", "℃"),
                     "overheat_gj": ("过热挖潜", "GJ")}
                return {"kind": "category", "title": "各站能效对标", "x": [r["name"] for r in rows],
                        "fields": {k: {"label": l, "unit": un, "values": [_num(r.get(k)) for r in rows]} for k, (l, un) in f.items()}}
            t = e["trend"]
            f = {"norm_heat": ("本期修正单耗", "kJ/(m²·℃·d)"), "base_norm_heat": ("去年同期修正单耗", "kJ/(m²·℃·d)"),
                 "tout": ("本期室外温度", "℃"), "base_tout": ("去年同期室外温度", "℃"), "heat_gj": ("供热量", "GJ")}
            return {"kind": "time", "title": "修正单耗趋势", "x": [r["date"] for r in t],
                    "fields": {k: {"label": l, "unit": un, "values": [_num(r.get(k)) for r in t]} for k, (l, un) in f.items()}}
        m = re.fullmatch(r"/api/ai/effect/([A-Za-z0-9]+)", path)
        if m:
            v.need("ai:read")
            sid = v.see(m.group(1))
            if sid not in STATION_BY_ID:
                raise VizError("BAD_REF", "调控效果只针对热力站")
            sw = q.get("switch")
            try:
                d = self.ai.control_effect(sid, datetime.fromisoformat(sw) if sw else None)["data"]
            except ValueError as ex:
                raise VizError("BAD_REF", str(ex)) from None
            s = d["series"]
            return {"kind": "time", "title": f"{d['station_name']}调控效果", "x": [r["date"] for r in s],
                    "fields": {"treat": {"label": "本站修正单耗", "unit": "kJ/(m²·℃·d)", "values": [r["treat"] for r in s]},
                               "control": {"label": "对照组修正单耗", "unit": "kJ/(m²·℃·d)", "values": [r["control"] for r in s]},
                               "tin": {"label": "本站室温", "unit": "℃", "values": [r["tin"] for r in s]}}}
        if path == "/api/realtime":
            v.need("monitor:read")
            snap = v.snapshot()
            sts = [s for s in STATIONS if s.id in snap["stations"]]
            return {"kind": "category", "title": "各站实时值", "x": [s.name for s in sts],
                    "fields": {k: {"label": l, "unit": un,
                                   "values": [_num(snap["stations"][s.id].get(k)) if snap["stations"][s.id]["online"] else None
                                              for s in sts]} for k, (l, un) in REALTIME_FIELDS.items()}}
        raise VizError("BAD_REF", "不支持的 data_ref。可用形式：" + "；".join(f.split("   ")[0] for f in DATA_REF_FORMS))

    # ================================================================ 规格校验
    def normalize(self, mode: str, title: str, spec: dict, v: Viewer) -> dict:
        if mode not in ("chart", "map", "twin"):
            raise VizError("BAD_MODE", "mode 须为 chart / map / twin")
        if not isinstance(spec, dict):
            raise VizError("BAD_SPEC", "spec 须为对象")
        title = _text(title, 60) or {"chart": "图表", "map": "管网定位", "twin": "工艺图定位"}[mode]
        if mode == "chart":
            _only(spec, {"type", "series", "y_label", "y_right_label", "mark_lines", "stack"}, "图表规格")
            typ = spec.get("type", "line")
            if typ not in CHART_TYPES:
                raise VizError("BAD_SPEC", f"type 须为 {'/'.join(CHART_TYPES)}")
            series = spec.get("series")
            if not isinstance(series, list) or not 1 <= len(series) <= MAX_SERIES:
                raise VizError("BAD_SPEC", f"series 须为 1–{MAX_SERIES} 条，每条 {{data_ref, field, name?, axis?}}；"
                                           "图表数据只能来自 data_ref，不接受内联数值")
            out, kinds = [], set()
            for s in series:
                if not isinstance(s, dict):
                    raise VizError("BAD_SPEC", "series 每项须为对象")
                _only(s, {"data_ref", "field", "name", "axis"}, "序列")
                ds = self.resolve(s.get("data_ref", ""), v)
                f = s.get("field")
                if f not in ds["fields"]:
                    raise VizError("BAD_SPEC", f"数据集「{ds['title']}」没有字段 {f}；可用：{'、'.join(ds['fields'])}")
                if s.get("axis", "left") not in ("left", "right"):
                    raise VizError("BAD_SPEC", "axis 须为 left / right")
                kinds.add(ds["kind"])
                out.append({"data_ref": s["data_ref"].strip(), "field": f, "axis": s.get("axis", "left"),
                            "name": _text(s.get("name"), 30) or ds["fields"][f]["label"]})
            if len(kinds) > 1:
                raise VizError("BAD_SPEC", "同一图表的序列须同为时间序列或同为分类数据")
            marks = spec.get("mark_lines") or []
            if not isinstance(marks, list) or len(marks) > MAX_MARKS:
                raise VizError("BAD_SPEC", f"mark_lines 最多 {MAX_MARKS} 条")
            mk = []
            for m in marks:
                if not isinstance(m, dict) or not isinstance(m.get("value"), (int, float)) or isinstance(m.get("value"), bool):
                    raise VizError("BAD_SPEC", "mark_lines 每项须为 {value: 数值, label?, axis?}")
                _only(m, {"value", "label", "axis"}, "参考线")
                mk.append({"value": float(m["value"]), "label": _text(m.get("label"), 20),
                           "axis": "right" if m.get("axis") == "right" else "left"})
            return {"mode": mode, "title": title, "type": typ, "series": out, "mark_lines": mk, "stack": bool(spec.get("stack")),
                    "y_label": _text(spec.get("y_label"), 20), "y_right_label": _text(spec.get("y_right_label"), 20),
                    "objects": sorted({_ref_object(s["data_ref"]) for s in out} - {None})}
        if mode == "map":
            _only(spec, {"object_ids", "color_by", "note", "level"}, "地图规格")
            ids = spec.get("object_ids") or []
            if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_OBJECTS:
                raise VizError("BAD_SPEC", f"object_ids 须为 1–{MAX_OBJECTS} 个热源/热力站 ID")
            ids = list(dict.fromkeys(v.see(str(i)) for i in ids))
            if "NET" in ids:
                raise VizError("BAD_SPEC", "object_ids 须为具体的热源或热力站")
            cb = spec.get("color_by") or "alarm"
            if cb not in COLOR_BY:
                raise VizError("BAD_SPEC", f"color_by 须为 {'/'.join(COLOR_BY)}")
            v.need("monitor:read")
            return {"mode": mode, "title": title, "object_ids": ids, "color_by": cb, "note": _text(spec.get("note"), 200),
                    "level": spec.get("level") if spec.get("level") in LEVELS else "info", "objects": ids}
        _only(spec, {"object_id", "parts", "note", "level"}, "工艺图规格")
        oid = v.see(str(spec.get("object_id") or ""))
        if oid == "NET":
            raise VizError("BAD_SPEC", "twin 须指定一个热源或热力站")
        allowed = STATION_PARTS if oid in STATION_BY_ID else SOURCE_PARTS
        parts = spec.get("parts") or []
        if not isinstance(parts, list) or any(p not in allowed for p in parts):
            raise VizError("BAD_SPEC", f"{_name(oid)} 的 parts 只能取 {'、'.join(f'{k}（{n}）' for k, n in allowed.items())}")
        v.need("monitor:read")
        return {"mode": mode, "title": title, "object_id": oid, "parts": list(dict.fromkeys(parts)),
                "part_names": [allowed[p] for p in dict.fromkeys(parts)], "note": _text(spec.get("note"), 200),
                "level": spec.get("level") if spec.get("level") in LEVELS else "info", "objects": [oid]}

    # ================================================================ 记录
    def create(self, *, mode: str, title: str, spec: dict, v: Viewer, owner: str, by: str, actor_kind: str) -> dict:
        norm = self.normalize(mode, title, spec, v)
        rec = {"viz_id": "VZ" + uuid.uuid4().hex[:8].upper(), "owner": owner, "created_by": by, "actor_kind": actor_kind,
               "created_at": now_local().isoformat(timespec="seconds"), "pinned": False, **norm}
        with self.lock:
            self.items[rec["viz_id"]] = rec
            mine = sorted((x for x in self.items.values() if x["owner"] == owner and not x["pinned"]),
                          key=lambda x: x["created_at"], reverse=True)
            for old in mine[KEEP_UNPINNED:]:
                self.items.pop(old["viz_id"], None)
            self._persist()
        return rec

    def get(self, vid: str, owner: str) -> dict:
        r = self.items.get(vid)
        if not r or r["owner"] != owner:              # 工作台是个人的；别人的记录按不存在处理
            raise VizError("NOT_FOUND", f"可视化 {vid} 不存在")
        return r

    def list(self, owner: str) -> list[dict]:
        return sorted((x for x in self.items.values() if x["owner"] == owner),
                      key=lambda x: (not x["pinned"], x["created_at"]), reverse=False)

    def pin(self, vid: str, owner: str, pinned: bool) -> dict:
        with self.lock:
            r = self.get(vid, owner)
            r["pinned"] = bool(pinned)
            self._persist()
            return r

    def delete(self, vid: str, owner: str):
        with self.lock:
            self.get(vid, owner)
            self.items.pop(vid)
            self._persist()

    def render(self, rec: dict, v: Viewer) -> dict:
        """用查看者身份取最新数据，组装前端渲染指令。图表各序列按 x 标签对齐。"""
        if rec["mode"] != "chart":
            for oid in rec["objects"]:
                v.see(oid)
            return {k: x for k, x in rec.items() if k != "objects"}
        cache, x, seen, series, kind = {}, [], set(), [], "time"
        for s in rec["series"]:
            if s["data_ref"] not in cache:
                cache[s["data_ref"]] = self.resolve(s["data_ref"], v)
            ds = cache[s["data_ref"]]
            f = ds["fields"][s["field"]]
            kind = ds["kind"]
            pos = {lbl: i for i, lbl in enumerate(ds["x"])}
            for lbl in ds["x"]:                       # x 取各数据集标签的并集，缺失处补空
                if lbl not in seen:
                    seen.add(lbl)
                    x.append(lbl)
            series.append({"name": s["name"], "axis": s["axis"], "unit": f["unit"], "_vals": f["values"], "_pos": pos})
        if kind == "time":
            x.sort(key=_time_key)
        out = []
        for s in series:
            out.append({"name": s["name"], "axis": s["axis"], "unit": s["unit"],
                        "data": [s["_vals"][s["_pos"][lbl]] if lbl in s["_pos"] else None for lbl in x]})
        return {k: val for k, val in rec.items() if k not in ("objects",)} | {"x": x, "x_kind": kind, "series_data": out,
                                                                            "rendered_at": now_local().isoformat(timespec="seconds")}

    def describe(self, rec: dict) -> str:
        if rec["mode"] == "chart":
            return f"{rec['type']} 图「{rec['title']}」：" + "、".join(s["name"] for s in rec["series"])
        if rec["mode"] == "map":
            return f"管网拓扑高亮 {'、'.join(_name(o) for o in rec['object_ids'])}（按{COLOR_BY[rec['color_by']][0]}着色）"
        return f"{_name(rec['object_id'])}工艺图高亮：" + ("、".join(rec["part_names"]) or "整体")


def _name(oid: str) -> str:
    return STATION_BY_ID[oid].name if oid in STATION_BY_ID else SOURCE_BY_ID[oid].name if oid in SOURCE_BY_ID else "全网"


def _num(x):
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return None if f != f or f in (float("inf"), float("-inf")) else round(f, 3)


def _ref_object(ref: str) -> str | None:
    m = re.match(r"/api/(?:history|ai/effect)/([A-Za-z0-9]+)", ref)
    return m.group(1).upper() if m else None


def _time_key(lbl: str):
    """x 标签为 “MM-DD HH:MM” 或 “MM-DD”；跨年（12 月→1 月）按采暖季顺序排。"""
    mm = int(lbl[:2]) if lbl[:2].isdigit() else 0
    return (0 if mm >= 7 else 1, lbl)
