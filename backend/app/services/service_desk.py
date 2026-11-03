"""客服：诉求分类、用户 360、回复草稿、通知（设计文档第七节 service_*）。

分类器是可解释的规则模型（依据示例文档 SVC-01 的分类与紧急标准），不是统计模型：
  · 每条判定都返回命中的证据，客服可以看到“为什么这样分”；
  · 拿不准、有升级风险、文本里夹带指令时一律转人工，不让自动流程处理；
  · 准确率在标注集上单独评测（python -m app.services.service_eval），开发集与留出集分开报告。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import unicodedata
import uuid
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from ..config import SOURCE_BY_ID, STATION_BY_ID
from ..sim.history import now_local
from .kb import injection_flags
from .ops import OpsError, mask_name, mask_phone, _iso

CATEGORIES = ["不热", "过热", "漏水", "收费", "报停与开栓", "其他"]
PRIORITY = ["漏水", "不热", "过热", "报停与开栓", "收费", "其他"]          # 多类时取最靠前者
DEPT = {"漏水": "维修", "不热": "维修", "过热": "维修", "收费": "营业", "报停与开栓": "营业"}
ROOM_STD, ROOM_URGENT, ROOM_HOT = 18.0, 16.0, 24.0

_P = lambda s: re.compile(s)  # noqa: E731
CAT_PATTERNS: dict[str, list[re.Pattern]] = {
    "漏水": [_P(r"漏水|滴水|渗水|跑水|漏了|渗到|渗漏|淌水|积水|往外(喷|冒|流)|喷水|爆管|爆了|裂了|冒.{0,2}气|一摊.{0,2}水")],
    "不热": [_P(r"不.{0,2}热(?!水)|没.{0,2}热乎|不暖|冰凉|凉凉|发凉|是凉的|下.{0,2}凉|冰窖|冷(?!静|水)|"
                r"(温度|室温)(太|偏|很|过|有点|有些|比较)?低|放气|没有?暖气")],
    "过热": [_P(r"(?<!不)(太|过|很|特别)热|热得|燥热|闷热|烫手|热死|(温度|室温)(太|偏|很|过|有点|有些|比较)?高|调低|降.{0,2}温度")],
    "收费": [_P(r"(取暖|供暖|采暖|热)费|交.{0,2}费|缴费|欠费|发票|收费|多少钱|退.{0,4}(费|钱|款)|分期|费用|贵了|价格|单价|一平米?|每平")],
    "报停与开栓": [_P(r"报停|停暖|停供|开栓|(恢复|重新)(供暖|供热|用暖)|没人住|不用暖气|空置")],
}
VULNERABLE = _P(r"老人|老母亲|老父亲|老太太|老爷子|老伴|[七八九]十(多)?岁?|宝宝|婴儿|新生儿|孩子|小孩|儿童|孕妇|怀孕|"
                r"病人|生病|发烧|卧床|瘫痪|残疾|坐月子|月子")
ESCALATE = _P(r"12345|市长(热线|信箱)|媒体|曝光|记者|电视台|报社|律师|起诉|法院|投诉|举报|信访|消协|315")
REFUND = _P(r"退.{0,4}(费|钱|款)|赔偿|索赔|赔钱|补偿")
REPEAT = _P(r"(打|来|反映|报)了?.{0,2}[两三四五六七八九十几好]+(次|遍|回)")
BULK_PII = _P(r"(所有|全部|批量|整栋|小区).{0,6}(用户|住户|客户|业主|人).{0,6}(电话|手机|号码|信息|资料|名单|身份证)")
BUILDING_WIDE = _P(r"整栋|整幢|整个楼|全楼|一栋楼都|整个单元|全单元|好几家|好多家|好几户|邻居们?都(说|不|没|反映|冷|一样)|大家都(说|不|没|冷)")
FILLER = _P(r"[喂嗯啊哦呃]+|你好|您好|那个|这个|就是|我想|想问|问一下|问问|请问|咨询|一下|麻烦|谢谢|[，。！？、,.!?\s~…]+")
TEMP = _P(r"(-?\d{1,2}(?:\.\d)?)\s*(?:度|°c|℃)")
TEMP_EXCLUDE = _P(r"供水|回水|水温|室外|外面|外头|气温|零下|调到|设定|设置|改成|暖气片|管子|管道|站")


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").lower()


def room_temps(text: str) -> list[float]:
    """抽取文本里的室温数字：按分句，排除供水温度、室外温度、设定值等上下文。"""
    out = []
    for clause in re.split(r"[，。！？；,;!?\n]", _norm(text)):
        if TEMP_EXCLUDE.search(clause):
            continue
        for m in TEMP.finditer(clause):
            v = float(m.group(1))
            if 5 <= v <= 35:
                out.append(v)
    return out


def classify_request(text: str) -> dict:
    t = _norm(text)
    hits: dict[str, list[str]] = {}
    for cat, pats in CAT_PATTERNS.items():
        ms = sorted({m.group(0) for p in pats for m in p.finditer(t)})
        if ms:
            hits[cat] = ms
    temps = room_temps(text)
    t_min = min(temps) if temps else None
    if "不热" not in hits and "过热" not in hits and t_min is not None:     # 只报了室温数字时按标准判断
        if t_min < ROOM_STD:
            hits["不热"] = [f"室温 {t_min:g}℃ 低于 {ROOM_STD:g}℃"]
        elif max(temps) >= ROOM_HOT:
            hits["过热"] = [f"室温 {max(temps):g}℃"]
    if "不热" in hits and "过热" in hits:          # “暖气不太热”之类同时命中时，以室温数字或否定词为准
        if t_min is not None and t_min >= ROOM_HOT:
            hits.pop("不热")
        else:
            hits.pop("过热")
    refund = REFUND.findall(t)
    reasons, human_reasons = [], []
    if refund:                                     # 退费、索赔诉求归收费，转人工
        hits.setdefault("收费", []).append("退费/赔偿")
        category = "收费"
    else:
        category = next((c for c in PRIORITY if c in hits), "其他")
    for c, ms in hits.items():
        reasons.append(f"{c}：命中「{'、'.join(ms[:4])}」")
    vulnerable = sorted({m.group(0) for m in VULNERABLE.finditer(t)})
    building_wide = bool(BUILDING_WIDE.search(t))
    urgent, urgent_reason = False, ""
    if category == "漏水" or "漏水" in hits:
        urgent, urgent_reason = True, "漏水诉求一律紧急（SVC-01 §1）"
    elif category == "不热" and vulnerable and t_min is not None and t_min < ROOM_URGENT:
        urgent, urgent_reason = True, f"家中有{'、'.join(vulnerable)}且室温 {t_min:g}℃ 低于 {ROOM_URGENT:g}℃（SVC-01 §1）"
    # ---- 转人工 ----
    flags = injection_flags(text)
    if BULK_PII.search(t):
        flags = sorted(set(flags) | {"索取他人个人信息"})
    if flags:
        human_reasons.append("文本含指令性内容：" + "、".join(flags))
    esc = sorted(set(ESCALATE.findall(t)))
    if esc:
        human_reasons.append(f"升级投诉风险：{'、'.join(esc)}")
    if refund:
        human_reasons.append("涉及退费或赔偿")
    if REPEAT.search(t):
        human_reasons.append("多次反映未解决")
    depts = {DEPT[c] for c in hits if c in DEPT}
    if len(depts) > 1:
        human_reasons.append(f"同时涉及{'、'.join(sorted(depts))}多个部门的诉求")
    if category == "其他" and len(FILLER.sub("", t)) < 4:
        human_reasons.append("表述不完整，无法归类")
    if building_wide and category == "不热":
        reasons.append("用户反映多户/整楼情况，建议核实后转调度（SVC-01 §2.3）")
    if category == "其他" and not human_reasons:
        reasons.append("未命中业务类别关键词")
    # 置信度不参与 category/urgent/human 判定（评测只评这三项）；未命中任何类别关键词时为“低”，
    # 业务流程把低置信诉求送人工复核，避免把没认出来的漏水、不热静默归为“其他”。
    confidence = "低" if not hits else "中" if len(hits) > 1 or category == "其他" else "高"
    return {"category": category, "urgent": urgent, "human": bool(human_reasons), "confidence": confidence,
            "matched": hits, "reasons": reasons, "urgent_reason": urgent_reason, "human_reasons": human_reasons,
            "room_temps": temps, "vulnerable": vulnerable, "building_wide_hint": building_wide, "injection_flags": flags,
            "method": "规则分类（SVC-01）：类别关键词 + 室温数字抽取；多类按 漏水>不热>过热>报停与开栓>收费 取主类别"}


WO_FOR = {"不热": "不热", "过热": "过热", "漏水": "漏水", "收费": "收费", "报停与开栓": "报停与开栓"}

NOTICE_KINDS = {
    "停热": "【供热通知】因{reason}，{scope}计划于 {start} 至 {end} 暂停供热，请提前做好防寒准备。如有疑问请致电客服热线。",
    "抢修": "【抢修通知】{scope}因{reason}正在抢修，供热可能受影响，预计 {end} 前恢复。给您带来不便，敬请谅解。",
    "恢复供热": "【恢复通知】{scope}已于 {start} 恢复供热，室温回升需要数小时。如仍有不热请致电客服热线。",
    "缴费提醒": "【缴费提醒】您的供热账户尚有欠费，请通过小程序或营业厅缴纳，以免影响正常供热服务。",
}


class ServiceDesk:
    def __init__(self, ops, kb, engine, control, data_dir: str | os.PathLike | None = None):
        self.ops, self.kb, self.engine, self.control = ops, kb, engine, control
        self.md = ops.md
        self.data_dir = Path(data_dir or os.environ.get("HEATNET_DATA_DIR") or Path(__file__).resolve().parents[2] / "data")
        self.lock = threading.RLock()
        self.requests: dict[str, dict] = {}
        self.notices: dict[str, dict] = {}
        p = self._path()
        if p.exists():
            d = json.loads(p.read_text(encoding="utf-8"))
            self.requests = {r["request_id"]: r for r in d.get("requests", [])}
            self.notices = {n["notice_id"]: n for n in d.get("notices", [])}

    def _path(self) -> Path:
        return self.data_dir / "service_state.json"

    def persist(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._path().with_suffix(".tmp")
        tmp.write_text(json.dumps({"requests": list(self.requests.values()), "notices": list(self.notices.values())},
                                  ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self._path())

    # ---------- 用户 360 ----------
    def _customer_or_raise(self, key: str) -> dict:
        c = self.md.lookup(key)
        if not c:
            raise OpsError("NOT_FOUND", "未找到该用户（可用户号、手机号或完整地址查询）")
        return c

    def customer_view(self, c: dict, reveal: bool) -> dict:
        return {"customer_id": c["customer_id"], "name": c["name"] if reveal else mask_name(c["name"]),
                "phone": c["phone"] if reveal else mask_phone(c["phone"]), "address": c["address"],
                "area_m2": c["area_m2"], "floor": c["floor"], "position": "、".join(
                    x for x, f in (("顶层", c["top"]), ("首层", c["ground"]), ("边户", c["edge"])) if f) or "中间户",
                "masked": not reveal}

    def customer_360(self, key: str, *, stations: set[str], reveal: bool = False) -> dict:
        c = self._customer_or_raise(key)
        if c["station_id"] not in stations:
            raise OpsError("OUT_OF_SCOPE", "该用户不在您的数据范围内")
        sid, bid = c["station_id"], c["building_id"]
        st = self.engine.snapshot["stations"][sid]
        b = self.md.buildings[bid]
        online = st.get("online", True)
        room = self.md.room_temp(c, st["tin"]) if online else None
        now = now_local()
        day_ago = _iso(now - timedelta(hours=24))
        wos = sorted((w for w in self.ops.workorders.values() if w["customer_id"] == c["customer_id"]),
                     key=lambda w: w["created_at"], reverse=True)
        bld_cold = [w for w in self.ops.workorders.values() if w["building_id"] == bid and w["type"] == "不热"
                    and w["created_at"] >= day_ago]
        srs = sorted((r for r in self.requests.values() if r.get("customer_id") == c["customer_id"]),
                     key=lambda r: r["created_at"], reverse=True)
        alarms = [a for a in self.engine.active_alarms() if a["object_id"] in (sid, STATION_BY_ID[sid].source_id)]
        notices = [n for n in self.notices.values() if n["status"] == "已发送" and n["scope_id"] in (bid, sid, STATION_BY_ID[sid].source_id)
                   and n["sent_at"] >= _iso(now - timedelta(days=3))]
        cmds = [x for x in self.control.list_commands(sid) if x["executed_at"] >= day_ago]
        facts = []
        if not online:
            facts.append("所属热力站当前通讯中断，实时数据不可用")
        elif room is not None:
            facts.append(f"按站典型室温与楼栋/户位置估算，该户室温约 {room:.1f}℃（{'达标' if room >= ROOM_STD else '低于 18℃ 标准'}）")
        if alarms:
            facts.append("所属站/热源有活动报警：" + "；".join(a["message"] for a in alarms[:3]))
        if len(bld_cold) >= 3:
            facts.append(f"同楼栋 24 小时内已有 {len(bld_cold)} 户报不热，属整楼问题（SVC-01 §2.3）")
        if cmds:
            facts.append(f"所属站近 24 小时调控 {len(cmds)} 次")
        if c["account"]["status"] != "正常":
            facts.append(f"账户状态：{c['account']['status']}")
        return {
            "customer": self.customer_view(c, reveal),
            "account": c["account"],
            "building": {"building_id": bid, "name": b["name"], "floors": b["floors"], "households": b["households"],
                         "cold_reports_24h": len(bld_cold)},
            "station": {"station_id": sid, "name": STATION_BY_ID[sid].name, "source": SOURCE_BY_ID[STATION_BY_ID[sid].source_id].name,
                        "online": online, "typical_tin": st["tin"] if online else None, "ts2": st["ts2"] if online else None,
                        "tr2": st["tr2"] if online else None, "ts": self.engine.snapshot.get("ts")},
            "room_temp_est": room, "room_temp_note": "估算值，非该户实测；以上门测温为准",
            "workorders": [{k: w[k] for k in ("workorder_id", "type", "status", "created_at", "due_at", "assignee_name")} for w in wos[:10]],
            "requests": [{k: r[k] for k in ("request_id", "text", "created_at", "status")} | {"category": r["classification"]["category"]}
                         for r in srs[:10]],
            "alarms": [{k: a[k] for k in ("object_name", "level", "message")} for a in alarms],
            "recent_notices": [{k: n[k] for k in ("notice_id", "kind", "content", "sent_at")} for n in notices],
            "facts": facts,
        }

    # ---------- 诉求登记与分类 ----------
    def register(self, text: str, *, channel: str, customer_key: str | None, by: str, stations: set[str]) -> dict:
        text = (text or "").strip()
        if len(text) < 2:
            raise OpsError("BAD_TEXT", "诉求内容不能为空")
        c = None
        if customer_key:
            c = self._customer_or_raise(customer_key)
            if c["station_id"] not in stations:
                raise OpsError("OUT_OF_SCOPE", "该用户不在您的数据范围内")
        cls = classify_request(text)
        with self.lock:
            # 同一用户 1 小时内同类诉求合并
            if c:
                hour_ago = _iso(now_local() - timedelta(hours=1))
                for r in self.requests.values():
                    if r.get("customer_id") == c["customer_id"] and r["created_at"] >= hour_ago \
                            and r["classification"]["category"] == cls["category"] and r["status"] != "已办结":
                        r["repeats"] = r.get("repeats", 0) + 1
                        r["log"].append({"ts": _iso(now_local()), "by": by, "action": f"重复来电：{text[:60]}"})
                        self.persist()
                        return {**r, "merged": True}
            r = {"request_id": "SR" + uuid.uuid4().hex[:8].upper(), "text": text[:1000], "channel": channel,
                 "customer_id": c["customer_id"] if c else None, "station_id": c["station_id"] if c else None,
                 "building_id": c["building_id"] if c else None, "classification": cls,
                 "status": "待人工" if cls["human"] or cls["confidence"] == "低" else "待处理", "created_by": by, "created_at": _iso(now_local()),
                 "workorder_id": None, "handoff_id": None, "reply": None, "log": [], "repeats": 0}
            r["suggested_actions"] = self._suggest(r)
            self.requests[r["request_id"]] = r
            self.persist()
            return {**r, "merged": False}

    def _suggest(self, r: dict) -> list[dict]:
        cls = r["classification"]
        if cls["human"]:
            return [{"action": "转人工", "why": "；".join(cls["human_reasons"])}]
        if cls["confidence"] == "低":
            return [{"action": "人工复核", "why": "规则分类器未识别出业务类别，可能是没认出的漏水或不热，请人工判断"}]
        out = []
        cat = cls["category"]
        bld = r.get("building_id")
        cold = 0
        if bld:
            day_ago = _iso(now_local() - timedelta(hours=24))
            cold = sum(1 for w in self.ops.workorders.values() if w["building_id"] == bld and w["type"] == "不热" and w["created_at"] >= day_ago)
        if cat == "不热" and (cold >= 2 or cls["building_wide_hint"]):
            out.append({"action": "移交调度", "tool": "service_handoff_to_dispatch",
                        "why": f"同楼栋 24 小时内已有 {cold} 张不热工单" if cold >= 2 else "用户反映多户不热，核实后按整楼问题处理"})
        if cat in ("不热", "过热", "漏水"):
            out.append({"action": "创建维修工单", "tool": "ops_create_workorder",
                        "args": {"type": WO_FOR[cat], "priority": "紧急" if cls["urgent"] else "普通",
                                 "customer_id": r.get("customer_id")}})
        elif cat in ("收费", "报停与开栓"):
            out.append({"action": "营业厅办理/答复", "why": "按收费办法和报停规定答复，不需派维修"})
        out.append({"action": "生成回复草稿", "tool": "service_draft_reply"})
        return out

    def get_request(self, rid: str) -> dict:
        r = self.requests.get(rid)
        if not r:
            raise OpsError("NOT_FOUND", f"诉求 {rid} 不存在")
        return r

    def list_requests(self, *, stations: set[str], status: str | None = None, days: int = 7) -> dict:
        since = _iso(now_local() - timedelta(days=days))
        rows = [r for r in self.requests.values() if r["created_at"] >= since and (r["station_id"] in stations or r["station_id"] is None)
                and (not status or r["status"] == status)]
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return {"requests": rows, "total": len(rows), "by_category": dict(Counter(r["classification"]["category"] for r in rows)),
                "by_status": dict(Counter(r["status"] for r in rows)),
                "urgent": sum(1 for r in rows if r["classification"]["urgent"]),
                "human": sum(1 for r in rows if r["classification"]["human"])}

    def link(self, rid: str, *, workorder_id: str | None = None, handoff_id: str | None = None, by: str):
        with self.lock:
            r = self.get_request(rid)
            if workorder_id:
                r["workorder_id"] = workorder_id
                r["log"].append({"ts": _iso(now_local()), "by": by, "action": f"关联工单 {workorder_id}"})
            if handoff_id:
                r["handoff_id"] = handoff_id
                r["log"].append({"ts": _iso(now_local()), "by": by, "action": f"移交调度 {handoff_id}"})
            if r["status"] == "待处理":
                r["status"] = "处理中"
            self.persist()

    def close_request(self, rid: str, by: str, note: str) -> dict:
        with self.lock:
            r = self.get_request(rid)
            if len((note or "").strip()) < 2:
                raise OpsError("NOTE_REQUIRED", "办结须填写处理结果")
            r["status"] = "已办结"
            r["log"].append({"ts": _iso(now_local()), "by": by, "action": f"办结：{note.strip()}"})
            self.persist()
            return r

    # ---------- 回复草稿 ----------
    def draft_reply(self, rid: str, *, stations: set[str], by: str) -> dict:
        r = self.get_request(rid)
        if r["station_id"] and r["station_id"] not in stations:
            raise OpsError("OUT_OF_SCOPE", "该诉求不在您的数据范围内")
        cls = r["classification"]
        cat = cls["category"]
        facts, cites, cautions = [], [], []
        if r["customer_id"]:
            v = self.customer_360(r["customer_id"], stations=stations)
            facts = v["facts"]
            room = v["room_temp_est"]
        else:
            room = None
            cautions.append("未关联用户，无法查询运行情况；请先核实地址")
        wo = self.ops.workorders.get(r["workorder_id"]) if r["workorder_id"] else None
        query = {"不热": "不热诉求处理流程 室温标准", "过热": "室温高于 过量供热", "漏水": "紧急诉求 到场时限",
                 "收费": "收费政策 回复要点", "报停与开栓": "报停 开栓", "其他": "回复要点"}[cat]
        try:
            hits = self.kb.search(query, top_k=3)["results"]
        except Exception:  # noqa: BLE001
            hits = []
        cites = [{"doc_id": h["doc_id"], "section": h["section"], "title": h["title"], "sample": h["sample"]} for h in hits
                 if h["doc_id"] in ("SVC-01", "POL-02", "POL-01")][:2]
        lines = ["您好，您反映的情况我们已经记录。"]
        if cat == "不热":
            if room is not None:
                lines.append(f"经查询，您所在小区热力站目前运行正常，按监测数据估算您家室温约 {room:.0f}℃。"
                             if not any("报警" in f or "中断" in f for f in facts) else "经查询，您所在小区热力站目前有运行报警，我们会同步给调度核查。")
            if r.get("handoff_id"):
                lines.append("您所在楼栋已有多户反映，我们已按整楼问题转调度排查。")
            elif any("整楼" in f for f in facts):
                lines.append("您所在楼栋已有多户反映，我们会按整楼问题转调度排查。")
                cautions.append("同楼栋已多户报不热，建议先移交调度（service_handoff_to_dispatch）再发送回复")
            if wo:
                lines.append(f"已为您派发维修工单（{wo['workorder_id']}），维修人员会先电话联系您再上门，"
                             f"{'按紧急诉求 4 小时内到场' if wo['priority'] == '紧急' else '24 小时内与您联系'}。")
            else:
                lines.append("我们将安排维修人员与您联系，排查户内阀门、过滤器和散热器排气。")
            lines.append("供暖期卧室、起居室室温标准为不低于 18℃。")
        elif cat == "过热":
            lines.append("室温过高既不舒适也浪费能源，我们会核查所在站的供热参数；户内也可适当关小散热器阀门，尽量不开窗散热。")
        elif cat == "漏水":
            lines.append("漏水属于紧急情况，请先关闭暖气进水阀门、避开漏水处并注意防烫。")
            lines.append(f"抢修工单{'（' + wo['workorder_id'] + '）' if wo else ''}已按紧急处理，维修人员 4 小时内到场。")
        elif cat == "收费":
            lines.append("收费相关问题我们会按现行收费办法为您核实答复，请保留缴费凭证。")
            cautions.append("收费金额、政策条款须引用现行收费办法原文，草稿中不写具体金额")
        elif cat == "报停与开栓":
            lines.append("报停或开栓需本人携带身份证和房产证明到营业厅办理，具体截止时间以营业厅公告为准。")
            cautions.append("办理材料和截止日期请以营业厅现行规定为准后再发送")
        else:
            lines.append("我们会转相关部门核实后答复您。")
        lines.append("如有其他问题请随时联系我们。")
        if cls["human"]:
            cautions.insert(0, "该诉求已标记转人工：" + "；".join(cls["human_reasons"]) + "。请人工处理，草稿仅供参考")
        cautions.append("不承诺无法确定的修复时间（SVC-01 §4）；估算室温不是实测值，不在回复中当作测量结果")
        reply = "".join(lines)
        with self.lock:
            r["reply"] = {"text": reply, "by": by, "at": _iso(now_local()), "status": "草稿"}
            self.persist()
        return {"request_id": rid, "category": cat, "reply": reply, "facts_used": facts, "citations": cites,
                "cautions": cautions, "status": "草稿（须客服确认后人工发送）"}

    # ---------- 通知 ----------
    def recipients(self, kind: str, scope_id: str) -> list[dict]:
        cs = self.md.customers_in(scope_id)
        if kind == "缴费提醒":
            return [c for c in cs if c["account"]["status"] == "欠费"]
        return [c for c in cs if c["account"]["status"] != "报停"]

    def create_notice(self, *, kind: str, scope_id: str, reason: str, start: str | None, end: str | None, by: str,
                      actor_kind: str, stations: set[str], channel: str = "短信+小程序") -> dict:
        if kind not in NOTICE_KINDS:
            raise OpsError("BAD_KIND", f"通知类型须为 {list(NOTICE_KINDS)}")
        cs = self.recipients(kind, scope_id)
        if not cs:
            raise OpsError("NO_RECIPIENTS", "该范围没有符合条件的接收用户")
        if any(c["station_id"] not in stations for c in cs):
            raise OpsError("OUT_OF_SCOPE", "通知范围超出您的数据范围")
        if kind in ("停热", "抢修") and len((reason or "").strip()) < 2:
            raise OpsError("REASON_REQUIRED", "停热/抢修通知须写明原因")
        if kind in ("停热", "抢修", "恢复供热") and not (start or end):
            raise OpsError("TIME_REQUIRED", "须填写开始或预计恢复时间")
        warnings = []
        if kind == "停热" and start:
            try:
                lead = (datetime.fromisoformat(start) - now_local()).total_seconds() / 3600
                if lead < 24:
                    warnings.append(f"计划停热应提前 24 小时通知（SVC-01 §5），当前仅提前 {max(lead, 0):.1f} 小时")
            except ValueError:
                raise OpsError("BAD_TIME", "时间格式应为 YYYY-MM-DDTHH:MM") from None
        scope_name = (self.md.buildings[scope_id]["name"] if scope_id in self.md.buildings else
                      STATION_BY_ID[scope_id].name.removesuffix("站") + "小区" if scope_id in STATION_BY_ID else
                      SOURCE_BY_ID[scope_id].name + "供热区域")
        fmt = lambda s: (s or "").replace("T", " ")[:16]  # noqa: E731
        content = NOTICE_KINDS[kind].format(reason=(reason or "").strip(), scope=scope_name, start=fmt(start), end=fmt(end) or "另行通知")
        with self.lock:
            hour_ago = _iso(now_local() - timedelta(hours=1))
            for n in self.notices.values():
                if n["kind"] == kind and n["scope_id"] == scope_id and n["created_at"] >= hour_ago and n["status"] in ("待确认", "已发送"):
                    return {**n, "duplicate": True}
            n = {"notice_id": "NT" + uuid.uuid4().hex[:8].upper(), "kind": kind, "scope_id": scope_id, "scope_name": scope_name,
                 "content": content, "channel": channel, "recipients": len(cs), "reason": reason, "start": start, "end": end,
                 "status": "待确认", "created_by": by, "actor_kind": actor_kind, "created_at": _iso(now_local()),
                 "warnings": warnings, "confirmed_by": None, "sent_at": None, "delivery": None,
                 "flags": injection_flags(reason or "")}
            self.notices[n["notice_id"]] = n
            self.persist()
            return {**n, "duplicate": False}

    def confirm_notice(self, nid: str, by: str, approve: bool = True, comment: str = "") -> dict:
        with self.lock:
            n = self.notices.get(nid)
            if not n:
                raise OpsError("NOT_FOUND", f"通知 {nid} 不存在")
            if n["status"] != "待确认":
                raise OpsError("BAD_STATE", f"通知状态为 {n['status']}，不能重复处理")
            if not approve:
                n.update(status="已驳回", confirmed_by=by, comment=comment)
                self.persist()
                return n
            cs = self.recipients(n["kind"], n["scope_id"])
            # 模拟下发：按用户号哈希确定性地模拟约 2% 失败（停机、空号），失败的转人工外呼
            failed = [c for c in cs if int(hashlib.md5((c["customer_id"] + nid).encode()).hexdigest(), 16) % 50 == 0]
            n.update(status="已发送", confirmed_by=by, comment=comment, sent_at=_iso(now_local()),
                     delivery={"total": len(cs), "delivered": len(cs) - len(failed), "failed": len(failed),
                               "failed_samples": [c["customer_id"] for c in failed[:20]],
                               "note": "演示环境为模拟下发，未真实发送短信"})
            self.persist()
            return n

    def list_notices(self, *, stations: set[str]) -> list[dict]:
        def in_scope(n):
            sid = n["scope_id"]
            if sid in self.md.buildings:
                return self.md.buildings[sid]["station_id"] in stations
            if sid in STATION_BY_ID:
                return sid in stations
            return all(s.id in stations for s in STATION_BY_ID.values() if s.source_id == sid)
        return sorted((n for n in self.notices.values() if in_scope(n)), key=lambda n: n["created_at"], reverse=True)
