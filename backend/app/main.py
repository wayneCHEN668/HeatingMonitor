"""热网监控系统 API。

运行：uvicorn app.main:app --reload --port 8000
- 所有 REST 接口返回统一封套 {ok, summary, data, evidence, confidence, warnings, trace_id}；
- 除登录与健康检查外，所有接口都要求 Authorization: Bearer <令牌>，并按权限与数据范围强制过滤（见 app/auth.py）；
- 业务记录与审计中的操作人一律取自令牌，不信任请求体里的用户名。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta

import numpy as np
from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .auth import AGENT_PROFILES, PERMISSIONS, ROLES, AuthError, AuthService, Principal
from .config import CURVE_TOUT, LIMITS, METRICS, SOURCE_BY_ID, SOURCES, STATION_BY_ID, STATIONS
from .services.ai import AIService
from .services.analytics import Analytics, name_of
from .services.control import ControlError, ControlService
from .services.kb import KnowledgeBase
from .services.weather_svc import weather_report
from .sim.history import HistoryStore, now_local
from .sim.realtime import Engine

log = logging.getLogger("heatnet")
TICK = 2.0
DEMO = os.environ.get("HEATNET_DEMO", "1") != "0"


def clean(o):
    """把 numpy / NaN 转成可 JSON 序列化的原生类型。"""
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (float, np.floating)):
        f = float(o)
        return None if math.isnan(f) or math.isinf(f) else round(f, 4)
    if isinstance(o, datetime):
        return o.isoformat(timespec="seconds")
    return o


def env(data=None, summary: str = "", **kw):
    if isinstance(data, dict) and {"ok", "summary", "trace_id"} <= set(data):   # 已是封套
        return clean(data)
    return clean({"ok": True, "summary": summary, "data": data, "evidence": kw.get("evidence", []),
                  "confidence": kw.get("confidence"), "warnings": kw.get("warnings", []),
                  "trace_id": uuid.uuid4().hex[:12]})


def fail(status: int, code: str, message: str):
    return JSONResponse(status_code=status, content={
        "ok": False, "summary": message, "data": None,
        "error": {"code": code, "message": message, "retryable": False}, "trace_id": uuid.uuid4().hex[:12]})


class State:
    store: HistoryStore
    engine: Engine
    control: ControlService
    analytics: Analytics
    ai: AIService
    auth: AuthService
    kb: KnowledgeBase
    clients: dict[WebSocket, str]          # 连接 -> 令牌（每次推送前重新校验）


S = State()


# ================================================================ 认证与授权
def _token(request: Request) -> str | None:
    h = request.headers.get("authorization", "")
    return h[7:].strip() if h.lower().startswith("bearer ") else None


def current(request: Request) -> Principal:
    return S.auth.authenticate(_token(request))


def need(*perms: str, any_of: bool = False, human: bool = False):
    """依赖工厂：要求全部（或任一）权限；human=True 时只接受人工会话。"""
    def dep(p: Principal = Depends(current)) -> Principal:
        ok = any(p.has(x) for x in perms) if any_of else all(p.has(x) for x in perms)
        if not ok:
            missing = "、".join(PERMISSIONS[x] for x in perms if not p.has(x))
            raise AuthError(403, "FORBIDDEN", f"{p.label} 没有权限：{missing}")
        if human and p.kind != "human":
            raise AuthError(403, "HUMAN_REQUIRED", "该操作只能由人工在平台上完成，智能体无权执行")
        return p
    return dep


def see(p: Principal, oid: str) -> str:
    oid = oid.strip().upper()
    if oid not in STATION_BY_ID and oid not in SOURCE_BY_ID and oid != "NET":
        raise HTTPException(404, f"对象 {oid} 不存在")
    if not p.can_see(oid):
        raise AuthError(403, "OUT_OF_SCOPE", f"{oid} 不在 {p.name} 的数据范围内")
    return oid


def see_station(p: Principal, sid: str) -> str:
    sid = see(p, sid)
    if sid not in STATION_BY_ID:
        raise HTTPException(404, f"{sid} 不是热力站")
    return sid


def draft_view(d: dict, p: Principal) -> dict:
    """令牌只返回给草案所属用户（本人或代其工作的智能体），且须有下发权限。"""
    d = dict(d)
    if d.get("approval") and not (d.get("owner") == p.username and p.has("control:execute")):
        d["approval"] = {**d["approval"], "token": None}
    return d


def visible_draft(p: Principal, draft_id: str) -> dict:
    d = S.control.get_draft(draft_id.strip().upper())
    if not p.can_see(d["station_id"]):
        raise AuthError(403, "OUT_OF_SCOPE", "该草案不在数据范围内")
    return d


def visible_command(p: Principal, cid: str) -> dict:
    c = S.control._cmd(cid.strip().upper())
    if not p.can_see(c["station_id"]):
        raise AuthError(403, "OUT_OF_SCOPE", "该指令不在数据范围内")
    return c


def scoped_snapshot(p: Principal) -> dict:
    snap = S.engine.snapshot
    pending = [d for d in S.control.pending_approvals() if p.can_see(d["station_id"])]
    n_pending = len(pending) if (p.has("control:approve") or p.has("control:draft")) else 0
    if p.is_global:
        return {**snap, "pending_approvals": n_pending}
    vs, vh = set(p.visible_stations), set(p.visible_sources)
    st = {k: v for k, v in snap["stations"].items() if k in vs}
    src = {k: v for k, v in snap["sources"].items() if k in vh}
    objs = {k: v for k, v in snap["today"]["objects"].items() if k in vs or k in vh}
    today = {"heat_gj": 0.0, "elec_kwh": 0.0, "water_t": 0.0, "gas_nm3": 0.0, "steam_t": 0.0, "cost_yuan": 0.0}
    for k, d in objs.items():
        for m in (today if k in vh else ("elec_kwh", "water_t")):
            today[m] += d.get(m, 0.0)
    network = {
        "heat_mw": sum(s["heat_mw"] for s in src.values()),
        "capacity_mw": sum(s["capacity_mw"] for s in src.values()),
        "flow_tph": sum(s["g"] for s in src.values()),
        "tin_avg": float(np.mean([s["tin"] for s in st.values()])) if st else None,
        "online": sum(1 for s in st.values() if s["online"]), "total": len(st),
        "area": sum(STATION_BY_ID[k].area for k in st), "elec_price": snap["network"]["elec_price"],
    }
    return {**snap, "stations": st, "sources": src, "network": network,
            "alarms": [a for a in snap["alarms"] if p.can_see(a["object_id"])],
            "today": {"network": today, "objects": objs}, "pending_approvals": n_pending}


# ================================================================ 生命周期
async def ticker():
    last = time.time()
    while True:
        await asyncio.sleep(TICK)
        now = time.time()
        try:
            S.engine.tick(now - last)
        except Exception:  # noqa: BLE001
            log.exception("tick failed")
        last = now
        for ws, token in list(S.clients.items()):
            try:
                p = S.auth.authenticate(token)          # 令牌被撤销/过期后立即停止推送
                msg = json.dumps({"type": "snapshot", "data": clean(scoped_snapshot(p))}, ensure_ascii=False)
                await ws.send_text(msg)
            except AuthError as e:
                S.clients.pop(ws, None)
                with contextlib.suppress(Exception):
                    await ws.send_text(json.dumps({"type": "auth_error", "message": e.message}, ensure_ascii=False))
                    await ws.close(code=4401)
            except Exception:  # noqa: BLE001
                S.clients.pop(ws, None)


def warmup():
    """后台预热 AI 模型缓存，首屏打开 AI 页时无需等待训练。"""
    try:
        S.ai.diagnosis()
        S.ai.curve_overview()
    except Exception:  # noqa: BLE001
        log.exception("warmup failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    S.store = HistoryStore()
    S.engine = Engine(S.store)
    S.control = ControlService(S.engine)
    S.analytics = Analytics(S.store)
    S.ai = AIService(S.store, S.engine)
    S.auth = AuthService()
    S.kb = KnowledgeBase()
    S.clients = {}
    task = asyncio.create_task(ticker())
    threading.Thread(target=warmup, daemon=True).start()
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


app = FastAPI(title="热网监控系统 API", version="1.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.exception_handler(AuthError)
async def auth_error(_: Request, e: AuthError):
    return fail(e.status, e.code, e.message)


@app.exception_handler(ControlError)
async def control_error(_: Request, e: ControlError):
    return fail(400, e.code, e.message)


@app.exception_handler(ValueError)
async def value_error(_: Request, e: ValueError):
    return fail(400, "BAD_REQUEST", str(e))


# ================================================================ 认证接口
class LoginBody(BaseModel):
    username: str
    password: str


class AgentTokenBody(BaseModel):
    agent: str = ""
    profile: str = "dispatch"
    days: int = 30
    note: str = ""


@app.post("/api/auth/login")
def login(body: LoginBody):
    token, p = S.auth.login(body.username.strip(), body.password)
    S.control._audit("login", p.label, "-", {"username": p.username}, "ok")
    return env({"token": token, "user": p.public()}, f"欢迎，{p.name}")


@app.post("/api/auth/logout")
def logout(p: Principal = Depends(current)):
    if p.kind == "human":
        S.auth.logout(p)
    return env({"logged_out": True})


@app.get("/api/auth/me")
def me(p: Principal = Depends(current)):
    return env(p.public())


@app.get("/api/auth/demo-accounts")
def demo_accounts():
    if not DEMO:
        raise HTTPException(404)
    return env(S.auth.demo_accounts(), "演示账号（生产环境请设置 HEATNET_DEMO=0 关闭）")


@app.get("/api/auth/catalog")
def catalog(p: Principal = Depends(current)):
    return env({"permissions": PERMISSIONS,
                "roles": {k: {"name": v[0], "permissions": sorted(v[1])} for k, v in ROLES.items()},
                "agent_profiles": {k: {"name": v[0], "permissions": sorted(v[1])} for k, v in AGENT_PROFILES.items()}})


@app.get("/api/auth/agent-tokens")
def list_agent_tokens(p: Principal = Depends(need("agent:delegate", human=True))):
    return env(S.auth.list_agent_tokens(p))


@app.post("/api/auth/agent-tokens")
def create_agent_token(body: AgentTokenBody, p: Principal = Depends(need("agent:delegate", human=True))):
    token, rec = S.auth.issue_agent_token(p, body.agent, body.profile, body.days, body.note)
    S.control._audit("agent_token_issue", p.label, "-",
                     {"agent": rec["agent"], "profile": rec["profile"], "jti": rec["jti"][:8], "expires_at": rec["expires_at"]}, "ok")
    return env({"token": token, "record": rec}, "委托令牌只显示这一次，请妥善保存")


@app.delete("/api/auth/agent-tokens/{jti}")
def revoke_agent_token(jti: str, p: Principal = Depends(current)):
    rec = S.auth.revoke_agent_token(p, jti)
    S.control._audit("agent_token_revoke", p.label, "-", {"agent": rec["agent"], "jti": jti[:8]}, "revoked")
    return env(rec, "已撤销")


# ================================================================ 元数据与实时
@app.get("/api/meta")
def meta(p: Principal = Depends(need("monitor:read"))):
    vh, vs = set(p.visible_sources), set(p.visible_stations)
    return env({
        "sources": [{"id": s.id, "name": s.name, "kind": s.kind, "capacity_mw": s.capacity_mw, "xy": s.xy,
                     "units": s.units, "curve": s.curve,
                     "stations": [t.id for t in STATIONS if t.source_id == s.id and t.id in vs]}
                    for s in SOURCES if s.id in vh],
        "stations": [{"id": s.id, "name": s.name, "source_id": s.source_id, "area": s.area, "system": s.system,
                      "design_index": s.design_index, "xy": s.xy, "autonomy": s.autonomy, "address": s.address,
                      "pump_kw": s.pump_kw} for s in STATIONS if s.id in vs],
        "metrics": {k: {"name": v[0], "unit": v[1]} for k, v in METRICS.items()},
        "curve_tout": CURVE_TOUT, "limits": LIMITS,
        "scope": {"global": p.is_global, "name": name_of("NET", p.sources)},
    })


@app.get("/api/realtime")
def realtime(p: Principal = Depends(need("monitor:read"))):
    return env(scoped_snapshot(p))


@app.get("/api/realtime/{oid}")
def realtime_obj(oid: str, p: Principal = Depends(need("monitor:read"))):
    oid = see(p, oid)
    snap = S.engine.snapshot
    r = snap["stations"].get(oid) or snap["sources"].get(oid)
    if not r:
        raise HTTPException(404, f"对象 {oid} 没有实时记录")
    return env(r)


@app.websocket("/ws/realtime")
async def ws_realtime(ws: WebSocket, token: str = ""):
    await ws.accept()
    try:
        p = S.auth.authenticate(token)
        if not p.has("monitor:read"):
            raise AuthError(403, "FORBIDDEN", "没有实时监控权限")
    except AuthError as e:
        await ws.send_text(json.dumps({"type": "auth_error", "message": e.message}, ensure_ascii=False))
        await ws.close(code=4401)
        return
    S.clients[ws] = token
    await ws.send_text(json.dumps({"type": "snapshot", "data": clean(scoped_snapshot(p))}, ensure_ascii=False))
    try:
        while True:
            await ws.receive_text()   # 保持连接；客户端可发 ping
    except WebSocketDisconnect:
        S.clients.pop(ws, None)


@app.get("/api/history/{oid}")
def history(oid: str, hours: int = Query(48, ge=1, le=24 * 60), metrics: str = "",
            p: Principal = Depends(need("monitor:read"))):
    oid = see(p, oid)
    df = S.store.frame(oid, p.sources).iloc[-hours:]
    cols = [m for m in metrics.split(",") if m and m in df.columns] or [
        c for c in df.columns if c not in ("season", "valve_sat", "curve_offset", "tr1_true")]
    return env({"ts": [t.strftime("%m-%d %H:%M") for t in df.index],
                **{c: df[c].astype(float).round(3).tolist() for c in cols}})


@app.get("/api/weather")
def weather(hours: int = Query(72, ge=6, le=168), p: Principal = Depends(need("monitor:read"))):
    w = weather_report(S.store, hours)
    return env(w["data"], w["summary"], warnings=w["warnings"], evidence=w["evidence"])


@app.get("/api/alarms")
def alarms(history: bool = False, p: Principal = Depends(need("monitor:read"))):
    rows = S.engine.alarm_log[:200] if history else S.engine.active_alarms()
    return env([a for a in rows if p.can_see(a["object_id"])])


@app.post("/api/alarms/{alarm_id}/ack")
def ack(alarm_id: str, p: Principal = Depends(need("alarm:ack"))):
    a = S.engine.alarms.get(alarm_id)
    if not a:
        raise HTTPException(404, "报警不存在或已恢复")
    see(p, a["object_id"])
    S.engine.ack(alarm_id, p.label)
    return env({"id": alarm_id}, "已确认")


# ================================================================ 调控
class DraftBody(BaseModel):
    station_id: str
    param: str = Field(description="valve_opening | control_mode | supply_temp_setpoint | climate_curve | pump_frequency")
    value: object
    reason: str = ""
    source: str = "manual"


class DecisionBody(BaseModel):
    comment: str = ""


class ExecBody(BaseModel):
    token: str


class RollbackBody(BaseModel):
    reason: str = ""


class ApprovalRequestBody(BaseModel):
    reason: str = ""
    timeout_s: int = 900


class ToolCallBody(BaseModel):
    tool: str
    args: dict = {}
    ok: bool = True
    summary: str = ""
    trace_id: str = ""
    risk: str = ""
    client: str = ""


@app.get("/api/control/policy/{sid}")
def policy(sid: str, p: Principal = Depends(need("monitor:read"))):
    return env(S.control.policy(see_station(p, sid)))


@app.get("/api/control/settings/{sid}")
def settings(sid: str, p: Principal = Depends(need("monitor:read"))):
    return env(S.control.current_settings(see_station(p, sid)))


@app.post("/api/control/drafts")
def create_draft(body: DraftBody, p: Principal = Depends(need("control:draft"))):
    sid = see_station(p, body.station_id)
    source = "ai" if p.kind == "agent" else body.source
    d = S.control.draft(sid, body.param, body.value, p.label, body.reason, source, owner=p.username)
    return env(draft_view(d, p), f"已生成草案 {d['draft_id']}：{d['description']}")


@app.post("/api/control/drafts/{draft_id}/check")
def check(draft_id: str, p: Principal = Depends(need("control:draft"))):
    d = S.control.safety_check(visible_draft(p, draft_id)["draft_id"], p.label)
    extra = "，L3 站点已由安全网关自动签发令牌" if d["status"] == "approved" else ""
    return env(draft_view(d, p), f"安全校验结果：{d['check']['verdict']}{extra}")


@app.post("/api/control/drafts/{draft_id}/request-approval")
def request_approval(draft_id: str, body: ApprovalRequestBody, p: Principal = Depends(need("control:draft"))):
    d = S.control.request_approval(visible_draft(p, draft_id)["draft_id"], p.label, body.reason, body.timeout_s)
    if d["status"] == "approved":
        return env(draft_view(d, p), "该草案已签发令牌（L3 站点安全网关自动签发或已人工批准）")
    return env(draft_view(d, p), f"已提交审批 {d['approval_request']['approval_id']}，等待值班调度确认")


@app.post("/api/control/drafts/{draft_id}/approve")
def approve(draft_id: str, body: DecisionBody, p: Principal = Depends(need("control:approve", human=True))):
    d = S.control.approve(visible_draft(p, draft_id)["draft_id"], p.label, body.comment)
    return env(draft_view(d, p), "已签发确认令牌")


@app.post("/api/control/drafts/{draft_id}/reject")
def reject(draft_id: str, body: DecisionBody, p: Principal = Depends(need("control:approve", human=True))):
    return env(draft_view(S.control.reject(visible_draft(p, draft_id)["draft_id"], p.label, body.comment), p), "草案已驳回")


@app.post("/api/control/drafts/{draft_id}/execute")
def execute(draft_id: str, body: ExecBody, p: Principal = Depends(need("control:execute"))):
    c = S.control.execute(visible_draft(p, draft_id)["draft_id"], body.token, p.label)
    return env(c, f"指令 {c['command_id']} 已下发并收到设备回执")


@app.get("/api/control/drafts/{draft_id}")
def get_draft(draft_id: str, p: Principal = Depends(need("control:draft", "control:approve", any_of=True))):
    return env(draft_view(visible_draft(p, draft_id), p))


@app.get("/api/control/approvals")
def approvals(p: Principal = Depends(need("control:draft", "control:approve", any_of=True))):
    return env([draft_view(d, p) for d in S.control.pending_approvals() if p.can_see(d["station_id"])])


@app.get("/api/control/drafts")
def drafts(station_id: str | None = None, p: Principal = Depends(need("control:draft", "control:approve", any_of=True))):
    sid = see_station(p, station_id) if station_id else None
    return env([draft_view(d, p) for d in S.control.list_drafts(sid) if p.can_see(d["station_id"])])


@app.get("/api/control/commands")
def commands(station_id: str | None = None, p: Principal = Depends(need("monitor:read"))):
    sid = see_station(p, station_id) if station_id else None
    return env([c for c in S.control.list_commands(sid) if p.can_see(c["station_id"])])


@app.get("/api/control/commands/{cid}/effect")
def effect(cid: str, p: Principal = Depends(need("monitor:read"))):
    return env(S.control.monitor(visible_command(p, cid)["command_id"]))


@app.post("/api/control/commands/{cid}/rollback")
def rollback(cid: str, body: RollbackBody, p: Principal = Depends(need("control:execute"))):
    c = S.control.rollback(visible_command(p, cid)["command_id"], p.label, body.reason)
    return env(c, "已回滚到下发前设定")


# ================================================================ 审计
@app.get("/api/audit")
def audit(limit: int = 300, p: Principal = Depends(need("audit:read"))):
    rows = S.control.audit if p.is_global else [
        a for a in S.control.audit if a["target"] in ("-", "") or p.can_see(a["target"])]
    return env(rows[:limit])


@app.post("/api/audit/tool-calls")
def tool_call_audit(body: ToolCallBody, p: Principal = Depends(current)):
    if p.kind != "agent":
        raise AuthError(403, "AGENT_ONLY", "只有智能体委托令牌可以写入工具调用审计")
    args = {**body.args, **({"_client": body.client} if body.client else {})}
    S.control.record_tool_call(p.agent, p.name, body.tool, args, body.ok, body.summary, body.trace_id, body.risk)
    return env({"recorded": True})


# ================================================================ 统计分析
def _range(start: str | None, end: str | None, days: int):
    now = now_local()
    e = datetime.fromisoformat(end) if end else now
    s = datetime.fromisoformat(start) if start else e - timedelta(days=days)
    if s >= e:
        raise ValueError("开始时间须早于结束时间")
    return s, e


@app.get("/api/analytics/daily")
def daily(day: str | None = None, scope: str = "NET", p: Principal = Depends(need("analytics:read"))):
    scope = see(p, scope)
    return env(S.analytics.daily(date.fromisoformat(day) if day else None, scope, p.sources))


@app.get("/api/analytics/energy")
def energy(start: str | None = None, end: str | None = None, days: int = 7,
           p: Principal = Depends(need("analytics:read"))):
    return env(S.analytics.energy(*_range(start, end, days), p.sources))


@app.get("/api/analytics/multidim")
def multidim(metric: str = "heat_gj", level: str = "station", grain: str = "day",
             start: str | None = None, end: str | None = None, days: int = 7,
             p: Principal = Depends(need("analytics:read"))):
    return env(S.analytics.multidim(metric, level, grain, *_range(start, end, days), p.sources))


# ================================================================ AI
@app.get("/api/ai/forecast")
def forecast(scope: str = "NET", horizon: int = 72, p: Principal = Depends(need("ai:read"))):
    return env(S.ai.forecast(see(p, scope), horizon, p.sources))


@app.get("/api/ai/anomalies")
def anomalies(days: int = 7, p: Principal = Depends(need("ai:read"))):
    e = dict(S.ai.anomalies(days))
    if not p.is_global:
        ev = [x for x in e["data"]["events"] if p.can_see(x["station_id"])]
        e["data"] = {**e["data"], "events": ev}
        n = sum(1 for x in ev if not x["known_cause"])
        e["summary"] = f"近 {days} 天数据范围内检测到 {n} 个站点存在持续性运行异常。"
    return env(e)


@app.get("/api/ai/anomalies/{sid}/series")
def anomaly_series(sid: str, days: int = 10, p: Principal = Depends(need("ai:read"))):
    return env(S.ai.anomaly_series(see_station(p, sid), days))


@app.get("/api/ai/curve")
def curve_overview(p: Principal = Depends(need("ai:read"))):
    e = dict(S.ai.curve_overview())
    if not p.is_global:
        rows = [r for r in e["data"]["rows"] if p.can_see(r["station_id"])]
        total = sum(max(r["saving_gj_day"] or 0, 0) for r in rows)
        e["data"] = {"rows": rows, "total_gj_day": round(total, 1)}
        e["summary"] = f"数据范围内按一站一策调整曲线，预计可节热约 {total:.0f} GJ/天。"
    return env(e)


@app.get("/api/ai/curve/{sid}")
def curve(sid: str, target: float = 20.0, p: Principal = Depends(need("ai:read"))):
    return env(S.ai.curve_opt(see_station(p, sid), target))


@app.get("/api/ai/effect/{sid}")
def control_effect(sid: str, switch: str | None = None, p: Principal = Depends(need("ai:read"))):
    return env(S.ai.control_effect(see_station(p, sid), datetime.fromisoformat(switch) if switch else None))


@app.get("/api/ai/diagnosis")
def diagnosis(p: Principal = Depends(need("ai:read"))):
    return env(S.ai.diagnosis(p.sources))


# ================================================================ 知识库
class KbDocBody(BaseModel):
    title: str = Field(min_length=2, max_length=60)
    doc_type: str
    content: str
    version: str = ""


class KbSimilarBody(BaseModel):
    symptom: str = ""
    object_type: str | None = None
    alarm_codes: list[str] = []
    features: list[str] = []
    top_k: int = Field(3, ge=1, le=10)
    include_pending: bool = False


class KbCaseBody(BaseModel):
    event_id: str | None = Field(None, description="报警 ID（如 ST07-MAKEUP）或指令号（CMD…），服务端解析为事件上下文")
    object_id: str | None = None
    title: str
    symptom: str
    cause: str = ""
    steps: list[str]
    outcome: str
    alarm_codes: list[str] = []
    features: list[str] = []
    tags: list[str] = []


class KbReviewBody(BaseModel):
    approve: bool
    comment: str = ""


def _resolve_event(p: Principal, event_id: str | None, object_id: str | None) -> dict:
    """把报警 ID / 指令号解析成事件上下文，并检查数据范围。"""
    if event_id:
        eid = event_id.strip()
        if eid.upper().startswith("CMD"):
            c = visible_command(p, eid)
            return {"kind": "command", "event_id": c["command_id"], "object_id": c["station_id"],
                    "object_name": c["station_name"], "description": c["description"],
                    "occurred_at": c["executed_at"][:10], "alarm_codes": []}
        a = S.engine.alarms.get(eid) or next((x for x in S.engine.alarm_log if x["id"] == eid), None)
        if not a:
            raise HTTPException(404, f"事件 {eid} 不存在（支持报警 ID 或指令号）")
        see(p, a["object_id"])
        return {"kind": "alarm", "event_id": a["id"], "object_id": a["object_id"], "object_name": a["object_name"],
                "description": a["message"], "occurred_at": a["first_ts"][:10], "alarm_codes": [a["code"]]}
    if object_id:
        oid = see(p, object_id)
        name = STATION_BY_ID[oid].name if oid in STATION_BY_ID else SOURCE_BY_ID[oid].name if oid in SOURCE_BY_ID else "全网"
        return {"kind": "object", "event_id": None, "object_id": oid, "object_name": name, "alarm_codes": []}
    return {"kind": "none", "event_id": None, "object_id": None, "object_name": "", "alarm_codes": []}


def _case_visible(p: Principal, c: dict) -> bool:
    """已审核案例全员可见（知识共享）；待审核/驳回的只对审核人和提交人可见。"""
    return c["status"] == "approved" or p.has("kb:review") or c.get("submitted_by") == p.label


@app.get("/api/kb/stats")
def kb_stats(p: Principal = Depends(need("kb:read"))):
    return env(S.kb.stats())


@app.get("/api/kb/search")
def kb_search(q: str, types: str = "", top_k: int = Query(5, ge=1, le=20), p: Principal = Depends(need("kb:read"))):
    r = S.kb.search(q, [t for t in types.split(",") if t] or None, top_k)
    warnings = ["部分片段含指令性文字，已标记；知识库内容仅供参考，不构成操作指令"] if any(x["flags"] for x in r["results"]) else []
    return env(r, f"检索到 {len(r['results'])} 个相关片段", warnings=warnings)


@app.get("/api/kb/docs")
def kb_docs(p: Principal = Depends(need("kb:read"))):
    return env(S.kb.list_docs())


@app.get("/api/kb/docs/{doc_id}")
def kb_doc(doc_id: str, p: Principal = Depends(need("kb:read"))):
    return env(S.kb.get_doc(doc_id))


@app.post("/api/kb/docs")
def kb_upload(body: KbDocBody, p: Principal = Depends(need("kb:manage", human=True))):
    d = S.kb.add_document(body.content, title=body.title, doc_type=body.doc_type, uploaded_by=p.label, version=body.version)
    S.control._audit("kb_doc_upload", p.label, "-", {"doc_id": d["doc_id"], "title": d["title"], "flags": d["flags"]}, "ok")
    warnings = [f"文档中检测到疑似提示注入文字：{'、'.join(d['flags'])}，相关片段已标记"] if d["flags"] else []
    return env(d, f"已入库 {d['doc_id']}，切分为 {d['n_chunks']} 个片段", warnings=warnings)


@app.delete("/api/kb/docs/{doc_id}")
def kb_delete(doc_id: str, p: Principal = Depends(need("kb:manage", human=True))):
    r = S.kb.delete_document(doc_id)
    S.control._audit("kb_doc_delete", p.label, "-", {"doc_id": doc_id}, "ok")
    return env(r, "已删除")


@app.get("/api/kb/cases")
def kb_cases(status: str = "", p: Principal = Depends(need("kb:read"))):
    return env([c for c in S.kb.list_cases(status or None) if _case_visible(p, c)])


@app.get("/api/kb/cases/{case_id}")
def kb_case(case_id: str, p: Principal = Depends(need("kb:read"))):
    c = S.kb.get_case(case_id)
    if not _case_visible(p, c):
        raise AuthError(403, "FORBIDDEN", "该案例尚未审核通过")
    return env(c)


@app.post("/api/kb/cases/similar")
def kb_similar(body: KbSimilarBody, p: Principal = Depends(need("kb:read"))):
    r = S.kb.similar_cases(body.symptom, object_type=body.object_type, alarm_codes=body.alarm_codes,
                           features=body.features, top_k=body.top_k,
                           include_pending=body.include_pending and p.has("kb:review"))
    return env(r, f"找到 {len(r['cases'])} 个相似案例")


@app.post("/api/kb/cases")
def kb_save_case(body: KbCaseBody, p: Principal = Depends(need("kb:write"))):
    ev = _resolve_event(p, body.event_id, body.object_id)
    oid = ev["object_id"] or ""
    otype = "station" if oid.startswith("ST") else "source" if oid.startswith("HS") else "network"
    c = S.kb.save_case(title=body.title, symptom=body.symptom, cause=body.cause, steps=body.steps, outcome=body.outcome,
                       object_type=otype, object_name=ev["object_name"],
                       alarm_codes=sorted(set(body.alarm_codes) | set(ev["alarm_codes"])), features=body.features,
                       tags=body.tags, event=ev if ev["kind"] != "none" else None, submitted_by=p.label)
    S.control._audit("kb_case_submit", p.label, oid or "-", {"case_id": c["case_id"], "title": c["title"],
                                                             "event_id": ev.get("event_id"), "flags": c["flags"]}, "pending")
    warnings = [f"案例文本中检测到疑似提示注入文字：{'、'.join(c['flags'])}，审核时请重点核对"] if c["flags"] else []
    return env(c, f"案例 {c['case_id']} 已提交，等待专家审核", warnings=warnings)


@app.post("/api/kb/cases/{case_id}/review")
def kb_review(case_id: str, body: KbReviewBody, p: Principal = Depends(need("kb:review", human=True))):
    c = S.kb.review_case(case_id, body.approve, p.label, body.comment)
    S.control._audit("kb_case_review", p.label, "-", {"case_id": case_id, "approve": body.approve,
                                                      "comment": body.comment}, c["status"])
    return env(c, "已审核通过，案例进入相似案例检索" if body.approve else "已驳回")


@app.get("/api/kb/advice")
def kb_advice(alarm_id: str, p: Principal = Depends(need("kb:read", "monitor:read"))):
    """报警处置建议：相似历史案例 + 相关规程/手册章节。"""
    ev = _resolve_event(p, alarm_id, None)
    if ev["kind"] != "alarm":
        raise ValueError("仅支持报警 ID")
    otype = "station" if ev["object_id"].startswith("ST") else "source"
    cases = S.kb.similar_cases(ev["description"], object_type=otype, alarm_codes=ev["alarm_codes"], top_k=3)
    docs = S.kb.search(f"{ev['description']} {' '.join(ev['alarm_codes'])}", top_k=3)
    return env({"event": ev, "cases": cases["cases"], "docs": docs["results"]},
               f"{ev['object_name']}：{ev['description']}；找到 {len(cases['cases'])} 个相似案例、{len(docs['results'])} 个规程片段")


@app.get("/api/health")
def health():
    return {"ok": True, "ts": now_local().isoformat(timespec="seconds"), "clients": len(S.clients)}
