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
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from .auth import AGENT_PROFILES, PERMISSIONS, ROLES, AuthError, AuthService, Principal
from .config import CURVE_TOUT, LIMITS, METRICS, SOURCE_BY_ID, SOURCES, STATION_BY_ID, STATIONS
from .services.ai import AIService
from .services.analytics import Analytics, name_of
from .services.breaker import BreakerError, CircuitBreaker
from .services.control import ControlError, ControlService
from .services.embedding import load_embedder
from .services.kb import KnowledgeBase
from .services.ops import PRIORITY_SLA_H, WO_TYPES, MasterData, OpsError, OpsService
from .services.reports import CONTENT_PERMS, DEDUPE_MIN, REPORT_TYPES, ReportError, ReportService
from .services.service_desk import NOTICE_KINDS, ServiceDesk, classify_request
from .services.viz import COLOR_BY, DATA_REF_FORMS, SOURCE_PARTS, STATION_PARTS, Viewer, VizError, VizService
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
    breaker: CircuitBreaker
    ops: OpsService
    service: ServiceDesk
    reports: ReportService
    viz: VizService
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


def agent_breaker_guard(p: Principal, station_id: str, what: str):
    """熔断（L1）后，智能体不能做 R2/R3；人工操作照常。"""
    if p.kind == "agent" and S.breaker.is_tripped(station_id):
        net = S.breaker.network_of(station_id)
        raise ControlError("BREAKER_OPEN", f"{SOURCE_BY_ID[net].name}管网已熔断（自治等级降为 L1），智能体不能{what}，"
                                           f"请交由人工处理。熔断原因：{'；'.join(S.breaker.nets[net].reasons)}")


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
    breakers = {k: v for k, v in S.breaker.summary().items() if p.can_see(k)}
    if p.is_global:
        return {**snap, "pending_approvals": n_pending, "breakers": breakers}
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
            "today": {"network": today, "objects": objs}, "pending_approvals": n_pending, "breakers": breakers}


# ================================================================ 生命周期
async def ticker():
    last = time.time()
    while True:
        await asyncio.sleep(TICK)
        now = time.time()
        try:
            S.engine.tick(now - last)
            S.breaker.record_sample({k: v["online"] for k, v in S.engine.snapshot["stations"].items()}, now - last)
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


def load_vectors():
    """后台加载中文向量模型并挂接到知识库；加载前或失败时知识库按关键词检索工作。"""
    S.kb.embed_status = {"enabled": False, "reason": "向量模型加载中…"}
    emb, status = load_embedder()
    if emb is not None:
        S.kb.attach_embedder(emb, status)
        log.info("知识库向量检索已启用：%s（%s 维）", status["model"], status["dim"])
    else:
        S.kb.embed_status = status


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
    S.breaker = CircuitBreaker()
    S.control.breaker = S.breaker
    S.breaker.on_trip.append(S.control.on_breaker_trip)
    S.breaker.on_reset.append(lambda net, by: S.control._audit("breaker_reset", by, net, {}, "reset"))
    S.engine.alarm_hooks.append(lambda: [
        (1, "BREAKER", k, SOURCE_BY_ID[k].name, "管网已熔断，自治等级降为 L1：" + "；".join(v["reasons"]))
        for k, v in S.breaker.summary().items() if v["state"] == "tripped"])
    S.ops = OpsService(S.engine, S.control, S.ai, S.kb, S.breaker, MasterData())
    S.service = ServiceDesk(S.ops, S.kb, S.engine, S.control)
    S.viz = VizService(S.store, S.ai, S.analytics)
    S.reports = ReportService(S.analytics, S.ai, S.engine, S.control, S.breaker, S.ops, S.service, S.kb)
    S.clients = {}
    task = asyncio.create_task(ticker())
    threading.Thread(target=warmup, daemon=True).start()
    threading.Thread(target=load_vectors, daemon=True).start()
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


@app.exception_handler(BreakerError)
async def breaker_error(_: Request, e: BreakerError):
    return fail(404 if e.code == "NOT_FOUND" else 400, e.code, e.message)


@app.exception_handler(OpsError)
async def ops_error(_: Request, e: OpsError):
    return fail({"NOT_FOUND": 404, "OUT_OF_SCOPE": 403}.get(e.code, 400), e.code, e.message)


@app.exception_handler(ReportError)
async def report_error(_: Request, e: ReportError):
    return fail({"NOT_FOUND": 404, "OUT_OF_SCOPE": 403}.get(e.code, 400), e.code, e.message)


@app.exception_handler(VizError)
async def viz_error(_: Request, e: VizError):
    return fail({"NOT_FOUND": 404, "OUT_OF_SCOPE": 403, "FORBIDDEN": 403}.get(e.code, 400), e.code, e.message)


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
    d = S.control.draft(sid, body.param, body.value, p.label, body.reason, source, owner=p.username, actor_kind=p.kind)
    return env(draft_view(d, p), f"已生成草案 {d['draft_id']}：{d['description']}")


@app.post("/api/control/drafts/{draft_id}/check")
def check(draft_id: str, p: Principal = Depends(need("control:draft"))):
    d = S.control.safety_check(visible_draft(p, draft_id)["draft_id"], p.label)
    extra = "，L3 站点已由安全网关自动签发令牌" if d["status"] == "approved" else ""
    return env(draft_view(d, p), f"安全校验结果：{d['check']['verdict']}{extra}")


@app.post("/api/control/drafts/{draft_id}/request-approval")
def request_approval(draft_id: str, body: ApprovalRequestBody, p: Principal = Depends(need("control:draft"))):
    d0 = visible_draft(p, draft_id)
    agent_breaker_guard(p, d0["station_id"], "提交审批（R2）")
    d = S.control.request_approval(d0["draft_id"], p.label, body.reason, body.timeout_s)
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
    d0 = visible_draft(p, draft_id)
    agent_breaker_guard(p, d0["station_id"], "下发指令（R3）")
    c = S.control.execute(d0["draft_id"], body.token, p.label)
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
def kb_search(q: str, types: str = "", top_k: int = Query(5, ge=1, le=20), mode: str | None = None,
              p: Principal = Depends(need("kb:read"))):
    if mode not in (None, "", "bm25", "vector", "hybrid"):
        raise ValueError("mode 须为 bm25 / vector / hybrid")
    r = S.kb.search(q, [t for t in types.split(",") if t] or None, top_k, mode=mode or None)
    warnings = ["部分片段含指令性文字，已标记；知识库内容仅供参考，不构成操作指令"] if any(x["flags"] for x in r["results"]) else []
    if r["mode"] == "bm25" and S.kb.embed_status.get("reason"):
        warnings.append(f"当前仅关键词检索：{S.kb.embed_status['reason']}")
    return env(r, f"检索到 {len(r['results'])} 个相关片段（{'关键词+语义混合' if r['mode'] == 'hybrid' else r['mode']}）",
               warnings=warnings)


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


# ================================================================ 熔断
class BreakerBody(BaseModel):
    comment: str = ""


@app.get("/api/breaker")
def breaker_status(p: Principal = Depends(need("monitor:read"))):
    rows = []
    for net in p.visible_sources:
        st = S.breaker.state(net)
        st["name"] = SOURCE_BY_ID[net].name
        st["stations"] = [{"id": s.id, "name": s.name, "configured": s.autonomy,
                           "effective": S.breaker.effective_autonomy(s.id)}
                          for s in STATIONS if s.source_id == net and p.can_see(s.id)]
        rows.append(st)
    n = sum(1 for r in rows if r["state"] == "tripped")
    return env(rows, f"{n} 个管网处于熔断状态" if n else "所有管网正常")


@app.post("/api/breaker/{network_id}/reset")
def breaker_reset(network_id: str, body: BreakerBody, p: Principal = Depends(need("control:approve", human=True))):
    see(p, network_id)
    st = S.breaker.reset(network_id, p.label, body.comment)
    return env(st, f"{SOURCE_BY_ID[network_id].name}熔断已解除，恢复配置的自治等级")


@app.post("/api/breaker/{network_id}/trip")
def breaker_trip(network_id: str, body: BreakerBody, p: Principal = Depends(need("control:approve", human=True))):
    see(p, network_id)
    st = S.breaker.trip(network_id, p.label, body.comment)
    return env(st, f"已人工熔断 {SOURCE_BY_ID[network_id].name}，管网降为 L1")


# ================================================================ 运维
def _stations(p: Principal, object_id: str | None = None) -> list[str]:
    """按对象（NET/热源/热力站）展开为可见热力站列表。"""
    oid = see(p, object_id or "NET")
    if oid == "NET":
        return list(p.visible_stations)
    if oid in SOURCE_BY_ID:
        return [s.id for s in STATIONS if s.source_id == oid and p.can_see(s.id)]
    return [oid]


def _wo_visible(p: Principal, wid: str) -> dict:
    w = S.ops.get(wid.strip().upper())
    if not p.can_see(w["station_id"]):
        raise AuthError(403, "OUT_OF_SCOPE", "该工单不在数据范围内")
    return w


class WorkorderBody(BaseModel):
    object_id: str = ""
    type: str
    description: str
    priority: str = "普通"
    customer_id: str | None = None
    request_id: str | None = None
    auto_assign: bool = True


class AssignBody(BaseModel):
    staff_id: str


class StatusBody(BaseModel):
    status: str
    result: str = ""


class PatrolBody(BaseModel):
    object_id: str = "NET"
    date: str | None = None
    focus: str = "风险优先"
    max_stops: int = Field(6, ge=1, le=13)


@app.get("/api/ops/meta")
def ops_meta(p: Principal = Depends(need("ops:read", "service:read", any_of=True))):
    return env({"types": WO_TYPES, "priorities": PRIORITY_SLA_H, "notice_kinds": list(NOTICE_KINDS),
                "buildings": [{k: b[k] for k in ("building_id", "station_id", "name", "households")}
                              for b in S.ops.md.buildings.values() if p.can_see(b["station_id"])]})


@app.get("/api/ops/workorders")
def ops_workorders(status: str | None = None, type: str | None = None, days: int = Query(7, ge=1, le=60),
                   object_id: str | None = None, p: Principal = Depends(need("ops:read"))):
    oid = object_id.strip() if object_id else None
    if oid and (oid.upper() in STATION_BY_ID or oid.upper() in SOURCE_BY_ID):
        oid = see(p, oid)
    r = S.ops.query(stations=set(p.visible_stations), status=status or None, typ=type or None, days=days, object_id=oid)
    hot = [c for c in r["clusters"] if c["whole_building"]]
    return env(r, f"近 {days} 天工单 {r['total']} 张，未完成 {sum(v for k, v in r['by_status'].items() if k not in ('已完成', '已关闭'))} 张，"
                  f"超时 {r['overdue']} 张" + (f"；{len(hot)} 个楼栋疑似整楼不热" if hot else ""),
               warnings=[c["hint"] + f"：{c['object_name']}" for c in hot])


@app.get("/api/ops/workorders/{wid}")
def ops_workorder(wid: str, p: Principal = Depends(need("ops:read"))):
    return env(_wo_visible(p, wid))


@app.post("/api/ops/workorders")
def ops_create(body: WorkorderBody, p: Principal = Depends(need("ops:write"))):
    if body.customer_id:
        c = S.ops.md.customers.get(body.customer_id.strip())
        if not c:
            raise OpsError("NOT_FOUND", f"住户 {body.customer_id} 不存在")
        if not p.can_see(c["station_id"]):
            raise AuthError(403, "OUT_OF_SCOPE", "该住户不在数据范围内")
    else:
        oid = body.object_id.strip()
        sid = S.ops.md.buildings[oid]["station_id"] if oid in S.ops.md.buildings else see_station(p, oid)
        if not p.can_see(sid):
            raise AuthError(403, "OUT_OF_SCOPE", "该对象不在数据范围内")
    if body.request_id:
        body.request_id = _sr_visible(p, body.request_id)["request_id"]
    w, created = S.ops.create(object_id=body.object_id.strip(), typ=body.type, description=body.description,
                              priority=body.priority, source="智能体" if p.kind == "agent" else "平台", created_by=p.label,
                              customer_id=body.customer_id.strip() if body.customer_id else None,
                              request_id=body.request_id, auto_assign=body.auto_assign)
    if body.request_id:
        S.service.link(body.request_id, workorder_id=w["workorder_id"], by=p.label)
    S.control._audit("workorder_create" if created else "workorder_duplicate", p.label, w["station_id"],
                     {"workorder_id": w["workorder_id"], "type": w["type"], "priority": w["priority"]}, "ok")
    warnings = ["描述中含指令性文字，已标记，仅作为工单描述保存：" + "、".join(w["flags"])] if w["flags"] else []
    if not created:
        return env({**w, "created": False}, f"该对象已有未完成的同类工单 {w['workorder_id']}（{w['status']}），未重复创建",
                   warnings=warnings)
    msg = f"已创建工单 {w['workorder_id']}（{w['type']}，{w['priority']}，应于 {w['due_at'][5:16].replace('T', ' ')} 前到场）"
    msg += f"，已自动派给 {w['assignee_name']}" if w["assignee"] else "，待人工派单"
    return env({**w, "created": True}, msg, warnings=warnings)


@app.get("/api/ops/workorders/{wid}/suggest")
def ops_suggest(wid: str, p: Principal = Depends(need("ops:read"))):
    w = _wo_visible(p, wid)
    r = S.ops.suggest(w["workorder_id"])
    top = r["candidates"][0] if r["candidates"] else None
    return env(r, f"推荐 {top['name']}（{top['team']}，评分 {top['score']}）" if top else "无在班维修人员",
               evidence=[{"type": "method", "text": r["method"]}])


@app.post("/api/ops/workorders/{wid}/assign")
def ops_assign(wid: str, body: AssignBody, p: Principal = Depends(need("ops:write", human=True))):
    w = _wo_visible(p, wid)
    w = S.ops.assign(w["workorder_id"], body.staff_id, p.label)
    S.control._audit("workorder_assign", p.label, w["station_id"], {"workorder_id": w["workorder_id"], "staff": body.staff_id}, "ok")
    return env(w, f"已派给 {w['assignee_name']}")


@app.post("/api/ops/workorders/{wid}/status")
def ops_status(wid: str, body: StatusBody, p: Principal = Depends(need("ops:write", human=True))):
    w = _wo_visible(p, wid)
    w = S.ops.update_status(w["workorder_id"], body.status, p.label, body.result)
    S.control._audit("workorder_status", p.label, w["station_id"], {"workorder_id": w["workorder_id"], "status": body.status}, "ok")
    return env(w, f"工单 {w['workorder_id']} → {w['status']}")


@app.get("/api/ops/staff")
def ops_staff(p: Principal = Depends(need("ops:read"))):
    return env(S.ops.staff)


@app.post("/api/ops/patrol-plan")
def ops_patrol(body: PatrolBody, p: Principal = Depends(need("ops:read"))):
    r = S.ops.patrol_plan(stations=_stations(p, body.object_id), date=body.date, focus=body.focus, max_stops=body.max_stops)
    return env(r, f"{r['date']} 巡检 {len(r['route'])} 站，约 {r['distance_km']} km、{r['est_hours']} 小时；"
                  f"首站 {r['route'][0]['station_name'] if r['route'] else '无'}",
               evidence=[{"type": "method", "text": r["method"]}],
               warnings=[] if r["route"] else ["没有风险分 ≥1 的站，可改为常规巡检"])


@app.get("/api/ops/handover")
def ops_handover(shift_id: str | None = None, p: Principal = Depends(need("ops:read"))):
    sts = set(p.visible_stations)
    reqs = [r for r in S.service.requests.values() if r["station_id"] in sts or (r["station_id"] is None and p.is_global)]
    r = S.ops.handover(shift_id, sts, set(p.visible_sources), weather_report(S.store, 48), reqs)
    return env(r, r["summary"])


@app.get("/api/ops/handoffs")
def ops_handoffs(p: Principal = Depends(need("ops:read", "service:read", any_of=True))):
    rows = sorted((h for h in S.ops.handoffs.values() if p.can_see(h["station_id"])), key=lambda h: h["created_at"], reverse=True)
    return env(rows, f"未关闭移交 {sum(1 for h in rows if h['status'] != '已关闭')} 件")


@app.post("/api/ops/handoffs/{hid}/status")
def ops_handoff_status(hid: str, body: StatusBody, p: Principal = Depends(need("ops:write", human=True))):
    h = S.ops.handoffs.get(hid.strip().upper())
    if not h or not p.can_see(h["station_id"]):
        raise OpsError("NOT_FOUND", f"移交 {hid} 不存在")
    h = S.ops.update_handoff(h["handoff_id"], body.status, p.label, body.result)
    S.control._audit("handoff_" + body.status, p.label, h["station_id"], {"handoff_id": h["handoff_id"]}, "ok")
    return env(h, f"移交 {h['handoff_id']} → {h['status']}")


# ================================================================ 客服
class RequestBody(BaseModel):
    text: str
    channel: str = "热线"
    customer_key: str | None = None


class ClassifyBody(BaseModel):
    text: str


class HandoffBody(BaseModel):
    summary: str = ""


class NoteBody(BaseModel):
    note: str = ""


class NoticeBody(BaseModel):
    kind: str
    scope_id: str
    reason: str = ""
    start: str | None = None
    end: str | None = None
    send_now: bool = False


class ConfirmBody(BaseModel):
    approve: bool = True
    comment: str = ""


def _sr_visible(p: Principal, rid: str) -> dict:
    r = S.service.get_request(rid.strip().upper())
    if r["station_id"] and not p.can_see(r["station_id"]):
        raise AuthError(403, "OUT_OF_SCOPE", "该诉求不在数据范围内")
    if not r["station_id"] and not p.is_global:
        raise AuthError(403, "OUT_OF_SCOPE", "未关联用户的诉求只对全网账号可见")
    return r


@app.get("/api/service/customers/lookup")
def svc_customer(key: str, reveal: bool = False, p: Principal = Depends(need("service:read"))):
    if reveal and not (p.has("customer:pii") and p.kind == "human"):
        raise AuthError(403, "PII_FORBIDDEN", "查看完整姓名与电话需要人工会话且具有 customer:pii 权限")
    v = S.service.customer_360(key, stations=set(p.visible_stations), reveal=reveal)
    if reveal:
        S.control._audit("pii_reveal", p.label, v["station"]["station_id"], {"customer_id": v["customer"]["customer_id"]}, "ok")
    return env(v, f"{v['customer']['address']}：{v['station']['name']}，" + ("；".join(v["facts"]) or "运行正常"),
               confidence="估算" if v["room_temp_est"] is not None else None,
               warnings=["室温为按站典型室温与户位置估算的值，不是该户实测"] if v["room_temp_est"] is not None else [])


@app.post("/api/service/classify")
def svc_classify(body: ClassifyBody, p: Principal = Depends(need("service:read"))):
    r = classify_request(body.text)
    return env(r, f"{r['category']}{'（紧急）' if r['urgent'] else ''}{'，建议转人工' if r['human'] else ''}")


@app.post("/api/service/requests")
def svc_register(body: RequestBody, p: Principal = Depends(need("service:write"))):
    r = S.service.register(body.text, channel=body.channel, customer_key=body.customer_key, by=p.label,
                           stations=set(p.visible_stations))
    cls = r["classification"]
    S.control._audit("service_request", p.label, r["station_id"] or "-",
                     {"request_id": r["request_id"], "category": cls["category"], "urgent": cls["urgent"]}, "merged" if r["merged"] else "ok")
    warnings = []
    if cls["injection_flags"]:
        warnings.append("诉求文本含指令性内容（" + "、".join(cls["injection_flags"]) + "），只作为用户原话记录，不得执行")
    if cls["confidence"] == "低":
        warnings.append("规则分类器未识别出业务类别，已送人工复核")
    head = f"诉求 {r['request_id']}{'（1 小时内重复来电，已合并）' if r['merged'] else ''}：{cls['category']}"
    head += "，紧急" if cls["urgent"] else ""
    head += "，转人工" if cls["human"] else ""
    return env(r, head, warnings=warnings, confidence=cls["confidence"],
               evidence=[{"type": "rule", "text": x} for x in cls["reasons"] + cls["human_reasons"]])


@app.get("/api/service/requests")
def svc_requests(status: str | None = None, days: int = Query(7, ge=1, le=60), p: Principal = Depends(need("service:read"))):
    r = S.service.list_requests(stations=set(p.visible_stations), status=status or None, days=days)
    if not p.is_global:
        r["requests"] = [x for x in r["requests"] if x["station_id"]]
    return env(r, f"近 {days} 天诉求 {r['total']} 件，紧急 {r['urgent']} 件，转人工 {r['human']} 件")


@app.get("/api/service/requests/{rid}")
def svc_request(rid: str, p: Principal = Depends(need("service:read"))):
    return env(_sr_visible(p, rid))


@app.post("/api/service/requests/{rid}/reply-draft")
def svc_reply(rid: str, p: Principal = Depends(need("service:write"))):
    r = _sr_visible(p, rid)
    d = S.service.draft_reply(r["request_id"], stations=set(p.visible_stations), by=p.label)
    return env(d, "回复草稿已生成，须客服确认后人工发送", warnings=d["cautions"],
               evidence=[{"type": "doc", "text": f"{c['doc_id']} {c['section']}"} for c in d["citations"]])


@app.post("/api/service/requests/{rid}/handoff")
def svc_handoff(rid: str, body: HandoffBody, p: Principal = Depends(need("service:write"))):
    r = _sr_visible(p, rid)
    if not r["station_id"]:
        raise OpsError("NO_CUSTOMER", "诉求未关联用户，无法确定楼栋，请先核实地址")
    summary = body.summary.strip() or f"{S.ops.md.buildings[r['building_id']]['name']}用户反映：{r['text'][:80]}"
    h = S.ops.handoff(station_id=r["station_id"], building_id=r["building_id"], summary=summary, request_id=r["request_id"], by=p.label)
    S.service.link(r["request_id"], handoff_id=h["handoff_id"], by=p.label)
    S.control._audit("handoff_create", p.label, r["station_id"], {"handoff_id": h["handoff_id"], "request_id": r["request_id"]}, "ok")
    return env(h, f"已移交调度（{h['handoff_id']}，关联诉求 {len(h['related_requests'])} 件），等待调度接收")


@app.post("/api/service/requests/{rid}/close")
def svc_close(rid: str, body: NoteBody, p: Principal = Depends(need("service:write", human=True))):
    r = _sr_visible(p, rid)
    return env(S.service.close_request(r["request_id"], p.label, body.note), "诉求已办结")


@app.get("/api/service/notices")
def svc_notices(p: Principal = Depends(need("service:read"))):
    rows = S.service.list_notices(stations=set(p.visible_stations))
    return env(rows, f"待确认通知 {sum(1 for n in rows if n['status'] == '待确认')} 条")


@app.post("/api/service/notices")
def svc_notice(body: NoticeBody, p: Principal = Depends(need("service:notify"))):
    n = S.service.create_notice(kind=body.kind, scope_id=body.scope_id.strip(), reason=body.reason, start=body.start, end=body.end,
                                by=p.label, actor_kind=p.kind, stations=set(p.visible_stations))
    if n.get("duplicate"):
        return env(n, f"1 小时内已有同类通知 {n['notice_id']}（{n['status']}），未重复创建")
    S.control._audit("notice_create", p.label, body.scope_id, {"notice_id": n["notice_id"], "kind": n["kind"],
                                                               "recipients": n["recipients"]}, "pending")
    if body.send_now and p.kind == "human":
        n = S.service.confirm_notice(n["notice_id"], p.label)
        S.control._audit("notice_send", p.label, body.scope_id, {"notice_id": n["notice_id"]}, "sent")
        return env(n, f"已发送 {n['kind']}通知：送达 {n['delivery']['delivered']} 户，失败 {n['delivery']['failed']} 户",
                   warnings=n["warnings"])
    extra = "智能体发起的通知须由客服人工确认后才会发送" if p.kind == "agent" else "请确认后发送"
    return env(n, f"{n['kind']}通知已生成（{n['notice_id']}，{n['recipients']} 户），{extra}", warnings=n["warnings"])


@app.post("/api/service/notices/{nid}/confirm")
def svc_notice_confirm(nid: str, body: ConfirmBody, p: Principal = Depends(need("service:notify", human=True))):
    nid = nid.strip().upper()
    if not any(n["notice_id"] == nid for n in S.service.list_notices(stations=set(p.visible_stations))):
        raise OpsError("NOT_FOUND", f"通知 {nid} 不存在")
    n = S.service.confirm_notice(nid, p.label, body.approve, body.comment)
    S.control._audit("notice_send" if body.approve else "notice_reject", p.label, n["scope_id"], {"notice_id": nid}, n["status"])
    return env(n, f"已发送：送达 {n['delivery']['delivered']} 户，失败 {n['delivery']['failed']} 户（失败的转人工外呼）"
               if body.approve else "通知已驳回")


# ================================================================ 报告
class ReportBody(BaseModel):
    report_type: str
    scope_id: str = "NET"
    period: str | None = None
    event_id: str | None = None
    force: bool = False


class ReviewBody(BaseModel):
    approve: bool = True
    conclusion: str = ""


def _report_visible(p: Principal, r: dict) -> bool:
    """报告是数据快照：只有能看到报告覆盖的全部对象的人才能看；事故报告含审计记录，另需 audit:read。"""
    if r["type"] == "incident" and not p.has("audit:read"):
        return False
    # 报告内容随生成者权限包含工单/诉求/异常检测章节；查看者须具备同样的权限
    if not set(r["_key"][4]) <= {x for x in CONTENT_PERMS if p.has(x)}:
        return False
    sid = r["scope_id"]
    if sid in STATION_BY_ID or sid in SOURCE_BY_ID:
        return p.can_see(sid)
    srcs = r["_key"][3]
    return p.is_global if srcs is None else set(srcs) <= set(p.visible_sources)


def _report_meta(r: dict) -> dict:
    return {**{k: r.get(k) for k in ("report_id", "type", "type_name", "title", "scope_id", "scope_name", "generated_at",
                                      "generated_by", "actor_kind", "status", "key_findings", "fingerprint", "event_id")},
            "period": r["period"]["label"], "check_ok": r["check"]["ok"], "numbers_checked": r["check"]["numbers_checked"],
            "link": f"/reports/{r['report_id']}", "html_url": f"/api/reports/{r['report_id']}/html"}


def _get_report(p: Principal, rid: str) -> dict:
    r = S.reports.get(rid.strip().upper())
    if not _report_visible(p, r):
        raise AuthError(403, "OUT_OF_SCOPE", "该报告超出您的数据范围或权限")
    return r


@app.get("/api/reports/meta")
def report_meta(p: Principal = Depends(need("report:read"))):
    return env({"types": REPORT_TYPES, "can_incident": p.has("audit:read") and p.has("report:write")})


@app.get("/api/reports")
def report_list(type: str | None = None, p: Principal = Depends(need("report:read"))):
    rows = sorted((r for r in S.reports.reports.values() if _report_visible(p, r) and (not type or r["type"] == type)),
                  key=lambda r: r["generated_at"], reverse=True)
    return env([_report_meta(r) for r in rows[:200]], f"共 {len(rows)} 份报告")


@app.post("/api/reports")
def report_generate(body: ReportBody, p: Principal = Depends(need("report:write"))):
    if body.report_type == "incident":
        if not (p.has("audit:read") and p.has("monitor:read")):
            raise AuthError(403, "FORBIDDEN", "事故报告包含审计记录，需要 audit:read 权限（值班调度长/管理员）")
        scope = "NET"
    else:
        if not p.has("analytics:read"):
            raise AuthError(403, "FORBIDDEN", f"{p.label} 没有权限：{PERMISSIONS['analytics:read']}")
        scope = see(p, body.scope_id)
    t0 = time.time()
    r = S.reports.generate(rtype=body.report_type, scope=scope, period=body.period, event_id=body.event_id,
                           sources=p.sources, by=p.label, actor_kind=p.kind, can_see=p.can_see, has=p.has, force=body.force)
    if not r["reused"]:
        S.control._audit("report_generate", p.label, r["scope_id"], {"report_id": r["report_id"], "type": r["type"],
                                                                    "period": r["period"]["label"]}, "ok")
    meta = _report_meta(r)
    msg = (f"{r['title']}：{f'复用 {DEDUPE_MIN} 分钟内已生成的同一报告' if r['reused'] else '已生成'}，"
           f"正文 {r['check']['numbers_checked']} 个数字全部核对通过（{time.time() - t0:.1f}s）")
    return env({**meta, "reused": r["reused"], "warnings": r["warnings"]}, msg, warnings=r["warnings"],
               evidence=[{"type": "fingerprint", "text": f"数据指纹 {r['fingerprint']}"}])


@app.get("/api/reports/{rid}")
def report_get(rid: str, p: Principal = Depends(need("report:read"))):
    r = _get_report(p, rid)
    return env({k: v for k, v in r.items() if k != "_key"} | {"link": f"/reports/{r['report_id']}"})


@app.get("/api/reports/{rid}/html", response_class=HTMLResponse)
def report_html(rid: str, p: Principal = Depends(need("report:read"))):
    r = _get_report(p, rid)
    return HTMLResponse(S.reports.render_html(r), headers={
        "Content-Disposition": f"inline; filename={r['report_id']}.html", "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:"})


@app.post("/api/reports/{rid}/review")
def report_review(rid: str, body: ReviewBody, p: Principal = Depends(need("report:review", human=True))):
    r = _get_report(p, rid)
    r = S.reports.review(r["report_id"], by=p.label, approve=body.approve, conclusion=body.conclusion)
    S.control._audit("report_review", p.label, r["scope_id"], {"report_id": r["report_id"], "approve": body.approve}, r["status"])
    return env(_report_meta(r), f"报告 {r['report_id']} {r['status']}")


# ================================================================ 可视化（viz_render）
class VizBody(BaseModel):
    mode: str
    title: str = ""
    spec: dict = Field(default_factory=dict)


class PinBody(BaseModel):
    pinned: bool = True


def _viewer(p: Principal) -> Viewer:
    return Viewer(p.sources, p.can_see, p.has, lambda: scoped_snapshot(p))


def _viz_meta(r: dict) -> dict:
    return {**{k: r.get(k) for k in ("viz_id", "mode", "title", "created_by", "actor_kind", "created_at", "pinned")},
            "description": S.viz.describe(r), "link": f"/viz/{r['viz_id']}"}


@app.get("/api/viz/meta")
def viz_meta(p: Principal = Depends(need("monitor:read"))):
    return env({"data_refs": DATA_REF_FORMS, "color_by": {k: v[0] for k, v in COLOR_BY.items()},
                "station_parts": STATION_PARTS, "source_parts": SOURCE_PARTS})


@app.post("/api/viz")
def viz_create(body: VizBody, p: Principal = Depends(need("monitor:read"))):
    rec = S.viz.create(mode=body.mode, title=body.title, spec=body.spec, v=_viewer(p), owner=p.username, by=p.label,
                       actor_kind=p.kind)
    return env(_viz_meta(rec), f"已生成：{S.viz.describe(rec)}；打开 /viz/{rec['viz_id']} 查看，可固定到工作台")


@app.get("/api/viz")
def viz_list(p: Principal = Depends(need("monitor:read"))):
    rows = [_viz_meta(r) for r in S.viz.list(p.username)]
    return env(rows, f"已固定 {sum(1 for r in rows if r['pinned'])} 个，共 {len(rows)} 个")


@app.get("/api/viz/{vid}")
def viz_get(vid: str, p: Principal = Depends(need("monitor:read"))):
    rec = S.viz.get(vid.strip().upper(), p.username)
    return env(S.viz.render(rec, _viewer(p)) | {"description": S.viz.describe(rec)})


@app.post("/api/viz/{vid}/pin")
def viz_pin(vid: str, body: PinBody, p: Principal = Depends(need("monitor:read", human=True))):
    rec = S.viz.pin(vid.strip().upper(), p.username, body.pinned)
    return env(_viz_meta(rec), "已固定到工作台" if rec["pinned"] else "已从工作台移除")


@app.delete("/api/viz/{vid}")
def viz_delete(vid: str, p: Principal = Depends(need("monitor:read", human=True))):
    S.viz.delete(vid.strip().upper(), p.username)
    return env({"deleted": True}, "已删除")


@app.get("/api/health")
def health():
    return {"ok": True, "ts": now_local().isoformat(timespec="seconds"), "clients": len(S.clients)}
