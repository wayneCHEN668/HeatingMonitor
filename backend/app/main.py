"""热网监控系统 API。

运行：uvicorn app.main:app --reload --port 8000
所有 REST 接口返回统一封套 {ok, summary, data, evidence, confidence, warnings, trace_id}，
便于前端统一处理，也便于后续直接包装成大模型 / MCP 工具。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta

import numpy as np
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .config import CURVE_TOUT, LIMITS, METRICS, SOURCE_BY_ID, SOURCES, STATION_BY_ID, STATIONS
from .services.ai import AIService
from .services.analytics import Analytics
from .services.control import ControlError, ControlService
from .sim.history import HistoryStore, now_local
from .sim.realtime import Engine

log = logging.getLogger("heatnet")
TICK = 2.0


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


class State:
    store: HistoryStore
    engine: Engine
    control: ControlService
    analytics: Analytics
    ai: AIService
    clients: set[WebSocket]


S = State()


async def ticker():
    last = time.time()
    while True:
        await asyncio.sleep(TICK)
        now = time.time()
        try:
            S.engine.tick(now - last)
            S.engine.snapshot["pending_approvals"] = len(S.control.pending_approvals())
        except Exception:  # noqa: BLE001
            log.exception("tick failed")
        last = now
        if S.clients:
            msg = json.dumps({"type": "snapshot", "data": clean(S.engine.snapshot)}, ensure_ascii=False)
            dead = []
            for ws in list(S.clients):
                try:
                    await ws.send_text(msg)
                except Exception:  # noqa: BLE001
                    dead.append(ws)
            for ws in dead:
                S.clients.discard(ws)


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
    S.clients = set()
    task = asyncio.create_task(ticker())
    threading.Thread(target=warmup, daemon=True).start()
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


app = FastAPI(title="热网监控系统 API", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.exception_handler(ControlError)
async def control_error(_: Request, e: ControlError):
    return JSONResponse(status_code=400, content={"ok": False, "summary": e.message, "data": None,
                                                  "error": {"code": e.code, "message": e.message, "retryable": False},
                                                  "trace_id": uuid.uuid4().hex[:12]})


@app.exception_handler(ValueError)
async def value_error(_: Request, e: ValueError):
    return JSONResponse(status_code=400, content={"ok": False, "summary": str(e), "data": None,
                                                  "error": {"code": "BAD_REQUEST", "message": str(e), "retryable": False},
                                                  "trace_id": uuid.uuid4().hex[:12]})


def _check_obj(oid: str):
    if oid not in STATION_BY_ID and oid not in SOURCE_BY_ID and oid != "NET":
        raise HTTPException(404, f"对象 {oid} 不存在")


# ---------------------------------------------------------------- 元数据
@app.get("/api/meta")
def meta():
    return env({
        "sources": [{"id": s.id, "name": s.name, "kind": s.kind, "capacity_mw": s.capacity_mw, "xy": s.xy,
                     "units": s.units, "curve": s.curve,
                     "stations": [t.id for t in STATIONS if t.source_id == s.id]} for s in SOURCES],
        "stations": [{"id": s.id, "name": s.name, "source_id": s.source_id, "area": s.area, "system": s.system,
                      "design_index": s.design_index, "xy": s.xy, "autonomy": s.autonomy, "address": s.address,
                      "pump_kw": s.pump_kw} for s in STATIONS],
        "metrics": {k: {"name": v[0], "unit": v[1]} for k, v in METRICS.items()},
        "curve_tout": CURVE_TOUT, "limits": LIMITS,
    })


# ---------------------------------------------------------------- 实时
@app.get("/api/realtime")
def realtime():
    return env(S.engine.snapshot)


@app.get("/api/realtime/{oid}")
def realtime_obj(oid: str):
    snap = S.engine.snapshot
    r = snap["stations"].get(oid) or snap["sources"].get(oid)
    if not r:
        raise HTTPException(404, f"对象 {oid} 不存在")
    return env(r)


@app.websocket("/ws/realtime")
async def ws_realtime(ws: WebSocket):
    await ws.accept()
    S.clients.add(ws)
    await ws.send_text(json.dumps({"type": "snapshot", "data": clean(S.engine.snapshot)}, ensure_ascii=False))
    try:
        while True:
            await ws.receive_text()   # 保持连接；客户端可发 ping
    except WebSocketDisconnect:
        S.clients.discard(ws)


@app.get("/api/history/{oid}")
def history(oid: str, hours: int = Query(48, ge=1, le=24 * 60), metrics: str = ""):
    _check_obj(oid)
    df = S.store.frame(oid).iloc[-hours:]
    cols = [m for m in metrics.split(",") if m and m in df.columns] or [
        c for c in df.columns if c not in ("season", "valve_sat", "curve_offset", "tr1_true")]
    return env({"ts": [t.strftime("%m-%d %H:%M") for t in df.index],
                **{c: df[c].astype(float).round(3).tolist() for c in cols}})


@app.get("/api/alarms")
def alarms(history: bool = False):
    return env(S.engine.alarm_log[:200] if history else S.engine.active_alarms())


class AckBody(BaseModel):
    user: str = "调度员"


@app.post("/api/alarms/{alarm_id}/ack")
def ack(alarm_id: str, body: AckBody):
    if not S.engine.ack(alarm_id, body.user):
        raise HTTPException(404, "报警不存在或已恢复")
    return env({"id": alarm_id}, "已确认")


# ---------------------------------------------------------------- 调控
class DraftBody(BaseModel):
    station_id: str
    param: str = Field(description="valve_opening | control_mode | supply_temp_setpoint | climate_curve | pump_frequency")
    value: object
    user: str = "调度员"
    reason: str = ""
    source: str = "manual"


class ApproveBody(BaseModel):
    approver: str = "值班调度长"
    comment: str = ""


class ExecBody(BaseModel):
    token: str
    user: str = "调度员"


class RollbackBody(BaseModel):
    user: str = "调度员"
    reason: str = ""


@app.get("/api/control/policy/{sid}")
def policy(sid: str):
    return env(S.control.policy(sid))


@app.get("/api/control/settings/{sid}")
def settings(sid: str):
    if sid not in STATION_BY_ID:
        raise HTTPException(404)
    return env(S.control.current_settings(sid))


@app.post("/api/control/drafts")
def create_draft(body: DraftBody):
    d = S.control.draft(body.station_id, body.param, body.value, body.user, body.reason, body.source)
    return env(d, f"已生成草案 {d['draft_id']}：{d['description']}")


@app.post("/api/control/drafts/{draft_id}/check")
def check(draft_id: str):
    d = S.control.safety_check(draft_id)
    v = d["check"]["verdict"]
    extra = "，L3 站点已由安全网关自动签发令牌" if d["status"] == "approved" else ""
    return env(d, f"安全校验结果：{v}{extra}")


@app.post("/api/control/drafts/{draft_id}/approve")
def approve(draft_id: str, body: ApproveBody):
    return env(S.control.approve(draft_id, body.approver, body.comment), "已签发确认令牌")


@app.post("/api/control/drafts/{draft_id}/reject")
def reject(draft_id: str, body: ApproveBody):
    return env(S.control.reject(draft_id, body.approver, body.comment), "草案已驳回")


@app.post("/api/control/drafts/{draft_id}/execute")
def execute(draft_id: str, body: ExecBody):
    c = S.control.execute(draft_id, body.token, body.user)
    return env(c, f"指令 {c['command_id']} 已下发并收到设备回执")


class ApprovalRequestBody(BaseModel):
    user: str = "调度智能体"
    reason: str = ""
    timeout_s: int = 900


class ToolCallBody(BaseModel):
    agent: str
    user: str
    tool: str
    args: dict = {}
    ok: bool = True
    summary: str = ""
    trace_id: str = ""
    risk: str = ""


@app.post("/api/control/drafts/{draft_id}/request-approval")
def request_approval(draft_id: str, body: ApprovalRequestBody):
    d = S.control.request_approval(draft_id, body.user, body.reason, body.timeout_s)
    if d["status"] == "approved":
        return env(d, "该草案已签发令牌（L3 站点安全网关自动签发或已人工批准）")
    return env(d, f"已提交审批 {d['approval_request']['approval_id']}，等待值班调度确认")


@app.get("/api/control/drafts/{draft_id}")
def get_draft(draft_id: str):
    return env(S.control.get_draft(draft_id))


@app.get("/api/control/approvals")
def approvals():
    return env(S.control.pending_approvals())


@app.post("/api/audit/tool-calls")
def tool_call_audit(body: ToolCallBody):
    S.control.record_tool_call(body.agent, body.user, body.tool, body.args, body.ok, body.summary,
                               body.trace_id, body.risk)
    return env({"recorded": True})


@app.get("/api/control/drafts")
def drafts(station_id: str | None = None):
    return env(S.control.list_drafts(station_id))


@app.get("/api/control/commands")
def commands(station_id: str | None = None):
    return env(S.control.list_commands(station_id))


@app.get("/api/control/commands/{cid}/effect")
def effect(cid: str):
    return env(S.control.monitor(cid))


@app.post("/api/control/commands/{cid}/rollback")
def rollback(cid: str, body: RollbackBody):
    return env(S.control.rollback(cid, body.user, body.reason), "已回滚到下发前设定")


@app.get("/api/audit")
def audit(limit: int = 200):
    return env(S.control.audit[:limit])


# ---------------------------------------------------------------- 统计分析
def _range(start: str | None, end: str | None, days: int):
    now = now_local()
    e = datetime.fromisoformat(end) if end else now
    s = datetime.fromisoformat(start) if start else e - timedelta(days=days)
    if s >= e:
        raise ValueError("开始时间须早于结束时间")
    return s, e


@app.get("/api/analytics/daily")
def daily(day: str | None = None, scope: str = "NET"):
    _check_obj(scope)
    return env(S.analytics.daily(date.fromisoformat(day) if day else None, scope))


@app.get("/api/analytics/energy")
def energy(start: str | None = None, end: str | None = None, days: int = 7):
    return env(S.analytics.energy(*_range(start, end, days)))


@app.get("/api/analytics/multidim")
def multidim(metric: str = "heat_gj", level: str = "station", grain: str = "day",
             start: str | None = None, end: str | None = None, days: int = 7):
    return env(S.analytics.multidim(metric, level, grain, *_range(start, end, days)))


# ---------------------------------------------------------------- AI
@app.get("/api/ai/forecast")
def forecast(scope: str = "NET", horizon: int = 72):
    _check_obj(scope)
    return env(S.ai.forecast(scope, horizon))


@app.get("/api/ai/anomalies")
def anomalies(days: int = 7):
    return env(S.ai.anomalies(days))


@app.get("/api/ai/anomalies/{sid}/series")
def anomaly_series(sid: str, days: int = 10):
    return env(S.ai.anomaly_series(sid, days))


@app.get("/api/ai/curve")
def curve_overview():
    return env(S.ai.curve_overview())


@app.get("/api/ai/curve/{sid}")
def curve(sid: str, target: float = 20.0):
    if sid not in STATION_BY_ID:
        raise HTTPException(404)
    return env(S.ai.curve_opt(sid, target))


@app.get("/api/ai/effect/{sid}")
def control_effect(sid: str, switch: str | None = None):
    if sid not in STATION_BY_ID:
        raise HTTPException(404, f"站点 {sid} 不存在")
    return env(S.ai.control_effect(sid, datetime.fromisoformat(switch) if switch else None))


@app.get("/api/ai/diagnosis")
def diagnosis():
    return env(S.ai.diagnosis())


@app.get("/api/health")
def health():
    return {"ok": True, "ts": now_local().isoformat(timespec="seconds"), "clients": len(S.clients)}
