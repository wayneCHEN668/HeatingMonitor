"""MCP 工具层端到端测试：真实 MCP 客户端（进程内）→ MCP 服务（委托令牌）→ FastAPI 后端（TestClient）。"""
import asyncio
import re

import pytest
from fastapi.testclient import TestClient
from mcp import Client

from app.main import app
from mcp_server import HeatnetAPI, build_server


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


def login(tc, user, pw):
    r = tc.post("/api/auth/login", json={"username": user, "password": pw})
    assert r.status_code == 200, r.text
    return r.json()["data"]["token"]


def delegate(tc, human_tok, profile="dispatch", agent="调度智能体"):
    r = tc.post("/api/auth/agent-tokens", headers=H(human_tok), json={"agent": agent, "profile": profile, "days": 7})
    assert r.status_code == 200, r.text
    return r.json()["data"]["token"]


@pytest.fixture(scope="module")
def tc():
    with TestClient(app) as c:
        c.sess = {u: login(c, u, pw) for u, pw in
                  [("dispatcher", "disp123"), ("chief", "chief123"), ("manager", "mgr123"), ("zhao", "zhao123"),
                   ("service", "svc123")]}
        c.agent = {
            "disp": delegate(c, c.sess["dispatcher"]),                              # 张调度 → 调度智能体
            "analyst": delegate(c, c.sess["manager"], "analyst", "经营分析智能体"),
            "zhao": delegate(c, c.sess["zhao"], "dispatch", "片区智能体"),          # 站长没有调控权限
            "service": delegate(c, c.sess["service"], "analyst", "客服智能体"),
            "chief": delegate(c, c.sess["chief"]),                                  # 调度长有批准权，但智能体拿不到
        }
        yield c


def run(tc, token, scenario):
    server = build_server(HeatnetAPI(client=tc, token=token))

    async def main():
        async with Client(server) as c:
            return await scenario(c)

    return asyncio.run(main())


async def names(c):
    return {t.name for t in (await c.list_tools()).tools}


async def call(c, name, **args):
    r = await c.call_tool(name, args)
    env = r.structured_content
    assert env is not None, f"{name} 没有结构化输出：{r.content}"
    assert {"ok", "summary", "data", "evidence", "warnings", "trace_id"} <= set(env)
    return env


# ---------------------------------------------------------------- 工具清单由令牌决定
def test_tool_catalog_dispatch(tc):
    async def s(c):
        return (await c.list_tools()).tools

    tools = run(tc, tc.agent["disp"], s)
    by = {t.name: t for t in tools}
    assert len(tools) == 20
    assert all(re.fullmatch(r"[a-z0-9_]+", n) for n in by)
    assert not any("approve" in n for n in by)
    assert by["control_execute"].annotations.destructive_hint is True
    assert by["data_get_realtime"].annotations.read_only_hint is True
    assert by["control_execute"].description.startswith("[R3")


def test_permission_intersection(tc):
    analyst = run(tc, tc.agent["analyst"], names)
    zhao = run(tc, tc.agent["zhao"], names)          # dispatch 档案 ∩ 站长权限 = 无调控
    service = run(tc, tc.agent["service"], names)    # 客服只有监控权限
    assert len(analyst) == 12 and not any(n.startswith("control_") for n in analyst)
    assert "kb_search" in analyst and "kb_save_case" not in analyst             # 分析智能体只读知识库
    assert not any(n.startswith("control_") for n in zhao)
    assert service == {"data_search_objects", "data_get_realtime", "data_get_timeseries_summary",
                       "data_get_alarms", "data_get_weather", "kb_search", "kb_find_similar_cases"}


def test_human_session_rejected_for_mcp(tc):
    with pytest.raises(ValueError, match="委托令牌"):
        build_server(HeatnetAPI(client=tc, token=tc.sess["dispatcher"]))


def test_scope_through_mcp(tc):
    async def s(c):
        srch = await call(c, "data_search_objects", keyword="东湖")
        rt = await call(c, "data_get_realtime", object_ids=["ST07", "ST01"])
        ts = await call(c, "data_get_timeseries_summary", object_id="ST01", metrics=["tin"])
        an = await call(c, "analyze_anomaly_detect")
        return srch, rt, ts, an

    srch, rt, ts, an = run(tc, tc.agent["zhao"], s)
    assert all(x["id"] in ("HS02", "ST07", "ST08", "ST09", "ST10") for x in srch["data"])
    assert set(rt["data"]["objects"]) == {"ST07"} and any("ST01" in w for w in rt["warnings"])
    assert not ts["ok"] and ts["error"]["code"] == "OUT_OF_SCOPE"
    assert {e["station_id"] for e in an["data"]["events"]} <= {"ST07", "ST08", "ST09", "ST10"}


# ---------------------------------------------------------------- 数据查询
def test_data_tools(tc):
    async def s(c):
        srch = [await call(c, "data_search_objects", keyword=k) for k in ("东湖3号站", "望湖街", "ST10", "锅炉房")]
        rt = await call(c, "data_get_realtime", object_ids=["ST02", "HS01", "NET", "ST99"])
        ts = await call(c, "data_get_timeseries_summary", object_id="ST02", metrics=["tin", "ts2", "nope"], hours=48)
        al = await call(c, "data_get_alarms", scope_id="HS02")
        wx = await call(c, "data_get_weather", hours_ahead=48, include_hourly=True)
        return srch, rt, ts, al, wx

    srch, rt, ts, al, wx = run(tc, tc.agent["disp"], s)
    assert [x["data"][0]["id"] for x in srch] == ["ST03", "ST03", "ST10", "HS02"]
    assert set(rt["data"]["objects"]) == {"ST02", "HS01", "NET"}
    t = ts["data"]["stats"]["tin"]
    assert t["min"] <= t["mean"] <= t["max"] and ts["data_ref"].startswith("/api/history/ST02")
    assert any(a["object_id"] == "ST07" and a["code"] == "MAKEUP" for a in al["data"]["alarms"])
    assert len(wx["data"]["forecast_hourly"]) == 48 and wx["data"]["forecast_daily"]
    assert "current_tout" in wx["data"]


def test_analysis_tools(tc):
    async def s(c):
        an = await call(c, "analyze_anomaly_detect")
        eb = await call(c, "analyze_energy_benchmark", days=7, top_n=3)
        fc = await call(c, "predict_heat_load", scope_id="HS01", horizon_hours=48, granularity="hour")
        ef = await call(c, "analyze_control_effect", station_id="ST01")
        return an, eb, fc, ef

    an, eb, fc, ef = run(tc, tc.agent["analyst"], s)
    found = {e["station_id"]: e["feature"] for e in an["data"]["events"]}
    assert found.get("ST03") == "heat_balance" and found.get("ST07") == "makeup"
    assert len(eb["data"]["worst"]) == 3 and eb["data"]["network"]["norm_heat"] > 0
    assert all(p["p10"] <= p["p50"] <= p["p90"] for p in fc["data"]["points"])
    assert ef["ok"] and ef["data"]["did_pct"] < 0


# ---------------------------------------------------------------- 调控链路
def test_ai_curve_flow_requires_human_approval(tc):
    state = {}

    async def part1(c):
        opt = await call(c, "optimize_station_curve", station_id="ST02")
        dr = await call(c, "control_draft_command", plan_ref=opt["data"]["plan_ref"], reason="室温偏高，按 AI 曲线下调")
        did = dr["data"]["draft_id"]
        early = await call(c, "control_request_approval", draft_id=did)
        chk = await call(c, "control_safety_check", draft_id=did)
        req = await call(c, "control_request_approval", draft_id=did, reason="请审批")
        forged = await call(c, "control_execute", draft_id=did, approval_token=f"{did}.9999999999.deadbeef")
        state["did"] = did
        return opt, dr, early, chk, req, forged

    opt, dr, early, chk, req, forged = run(tc, tc.agent["disp"], part1)
    did = state["did"]
    assert dr["data"]["value"] == opt["data"]["recommended_curve"]
    assert not early["ok"] and early["error"]["code"] == "NOT_CHECKED"
    assert chk["data"]["verdict"] in ("pass", "warn") and "approval_token" not in chk["data"]
    assert req["data"]["status"] == "pending_approval" and "approval_token" not in req["data"]
    assert not forged["ok"] and forged["error"]["code"] == "TOKEN_INVALID"

    # —— 谁都不能用智能体或无权会话绕过人工批准 ——
    url = f"/api/control/drafts/{did}/approve"
    assert tc.post(url, headers=H(tc.agent["disp"]), json={}).status_code == 403
    r = tc.post(url, headers=H(tc.agent["chief"]), json={})          # 调度长委托的智能体：白名单里没有批准权
    assert r.status_code == 403
    assert tc.post(url, headers=H(tc.sess["dispatcher"]), json={}).status_code == 403   # 调度员本人也没有
    r = tc.post(url, headers=H(tc.sess["chief"]), json={"comment": "同意"})           # 只有调度长人工会话可以
    assert r.status_code == 200 and r.json()["data"]["approval"]["token"] is None       # 令牌不返回给审批人
    assert r.json()["data"]["approval"]["approver"] == "李调度长（值班调度长）"

    async def part2(c):
        req2 = await call(c, "control_request_approval", draft_id=did)
        token = req2["data"]["approval_token"]
        ex = await call(c, "control_execute", draft_id=did, approval_token=token)
        reuse = await call(c, "control_execute", draft_id=did, approval_token=token)
        cid = ex["data"]["command_id"]
        mon = await call(c, "control_monitor_effect", command_id=cid)
        eff = await call(c, "analyze_control_effect", command_id=cid)
        rb = await call(c, "control_rollback", command_id=cid, reason="测试回滚")
        return req2, ex, reuse, mon, eff, rb

    req2, ex, reuse, mon, eff, rb = run(tc, tc.agent["disp"], part2)
    assert req2["data"]["status"] == "approved" and req2["data"]["approver"].startswith("李调度长")
    assert ex["ok"] and not reuse["ok"]
    assert "tracking" in mon["data"]
    assert not eff["ok"] and "12 小时" in eff["error"]["message"]
    assert rb["data"]["status"] == "rolled_back"

    cmd = next(c for c in tc.get("/api/control/commands", headers=H(tc.sess["chief"])).json()["data"]
               if c["command_id"] == ex["data"]["command_id"])
    assert cmd["executed_by"] == "调度智能体（代 张调度）" and cmd["source"] == "ai"
    audit = tc.get("/api/audit", headers=H(tc.sess["chief"])).json()["data"]
    calls = [a for a in audit if a["action"] == "tool_call"]
    assert {"control_execute", "control_request_approval", "optimize_station_curve"} <= {a["detail"]["tool"] for a in calls}
    assert all("approval_token" not in a["detail"]["args"] for a in calls)
    assert any(a["user"] == "调度智能体（代 张调度）" for a in calls)
    # 智能体不能伪造别人的工具调用审计；人工会话也不能写
    assert tc.post("/api/audit/tool-calls", headers=H(tc.sess["chief"]), json={"tool": "x"}).status_code == 403


def test_l3_station_auto_sign(tc):
    async def s(c):
        pol = await call(c, "control_get_autonomy_policy", station_id="ST01")
        curve = [[t, y - 0.5] for t, y in pol["data"]["current_settings"]["curve"]]
        dr = await call(c, "control_draft_command", station_id="ST01", param="climate_curve", value=curve, reason="微调")
        chk = await call(c, "control_safety_check", draft_id=dr["data"]["draft_id"])
        ex = await call(c, "control_execute", draft_id=dr["data"]["draft_id"], approval_token=chk["data"]["approval_token"])
        rb = await call(c, "control_rollback", command_id=ex["data"]["command_id"], reason="测试结束")
        return pol, chk, ex, rb

    pol, chk, ex, rb = run(tc, tc.agent["disp"], s)
    assert pol["data"]["policy"]["autonomy"] == "L3"
    assert chk["data"]["verdict"] == "pass" and chk["data"]["status"] == "approved"
    assert ex["ok"] and rb["ok"]


def test_blocked_draft_cannot_be_submitted(tc):
    async def s(c):
        pol = await call(c, "control_get_autonomy_policy", station_id="ST05")
        v = max(5, pol["data"]["current_settings"]["valve_set"] - 45)
        dr = await call(c, "control_draft_command", station_id="ST05", param="valve_opening", value=v, reason="测试")
        chk = await call(c, "control_safety_check", draft_id=dr["data"]["draft_id"])
        req = await call(c, "control_request_approval", draft_id=dr["data"]["draft_id"])
        bad = await call(c, "control_draft_command", reason="缺参数")
        return chk, req, bad

    chk, req, bad = run(tc, tc.agent["disp"], s)
    assert chk["data"]["verdict"] == "block"
    assert not req["ok"] and req["error"]["code"] == "BLOCKED"
    assert not bad["ok"] and bad["error"]["code"] == "BAD_ARGUMENT"


def test_revoked_token_stops_working(tc):
    tok = delegate(tc, tc.sess["dispatcher"], agent="临时智能体")
    jti = tc.get("/api/auth/agent-tokens", headers=H(tc.sess["dispatcher"])).json()["data"][0]["jti"]

    async def s(c):
        before = await call(c, "data_get_realtime", object_ids=["ST02"])
        r = tc.delete(f"/api/auth/agent-tokens/{jti}", headers=H(tc.sess["dispatcher"]))
        assert r.status_code == 200
        after = await call(c, "data_get_realtime", object_ids=["ST02"])
        return before, after

    before, after = run(tc, tok, s)
    assert before["ok"] and not after["ok"] and after["error"]["code"] == "TOKEN_REVOKED"
