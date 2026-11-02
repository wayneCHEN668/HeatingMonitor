"""MCP 工具层端到端测试：真实 MCP 客户端（进程内）→ MCP 服务 → FastAPI 后端（TestClient）。"""
import asyncio
import re

import pytest
from fastapi.testclient import TestClient
from mcp import Client

from app.main import app
from mcp_server import HeatnetAPI, build_server


@pytest.fixture(scope="module")
def tc():
    with TestClient(app) as c:
        yield c


def run(tc, scenario, **kw):
    server = build_server(HeatnetAPI(client=tc), **kw)

    async def main():
        async with Client(server) as c:
            return await scenario(c)

    return asyncio.run(main())


async def call(c, name, **args):
    r = await c.call_tool(name, args)
    env = r.structured_content
    assert env is not None, f"{name} 没有结构化输出：{r.content}"
    assert {"ok", "summary", "data", "evidence", "warnings", "trace_id"} <= set(env)
    return env


# ---------------------------------------------------------------- 工具清单
def test_tool_catalog(tc):
    async def s(c):
        return (await c.list_tools()).tools

    tools = run(tc, s)
    names = {t.name for t in tools}
    assert len(tools) == 16
    assert all(re.fullmatch(r"[a-z0-9_]+", n) for n in names)        # 兼容各家 Function Calling 命名
    assert not any("approve" in n for n in names)                       # 智能体没有批准工具
    by = {t.name: t for t in tools}
    assert by["control_execute"].annotations.destructive_hint is True
    assert by["data_get_realtime"].annotations.read_only_hint is True
    assert by["control_execute"].description.startswith("[R3")
    assert by["data_get_realtime"].output_schema["properties"]["summary"]["type"] == "string"


def test_analyst_profile_has_no_control(tc):
    async def s(c):
        names = {t.name for t in (await c.list_tools()).tools}
        r = await c.call_tool("control_execute", {"draft_id": "x", "approval_token": "y"})
        return names, r

    names, r = run(tc, s, profile="analyst")
    assert names and not any(n.startswith("control_") for n in names)
    assert r.is_error


# ---------------------------------------------------------------- 数据查询
def test_search_objects(tc):
    async def s(c):
        return [await call(c, "data_search_objects", keyword=k) for k in ("东湖3号站", "望湖街", "ST10", "锅炉房", "东湖")]

    a, b, d, e, f = run(tc, s)
    assert a["data"][0]["id"] == "ST03"
    assert b["data"][0]["id"] == "ST03"                     # 按地址
    assert d["data"][0]["id"] == "ST10" and d["data"][0]["match"] == 1.0
    assert e["data"][0]["id"] == "HS02"
    assert {x["id"] for x in f["data"]} >= {"ST02", "ST03"}  # 模糊：多个候选


def test_realtime_and_summary(tc):
    async def s(c):
        rt = await call(c, "data_get_realtime", object_ids=["ST02", "HS01", "NET", "ST99"])
        ts = await call(c, "data_get_timeseries_summary", object_id="ST02", metrics=["tin", "ts2", "nope"], hours=48)
        al = await call(c, "data_get_alarms", scope_id="HS02")
        return rt, ts, al

    rt, ts, al = run(tc, s)
    assert set(rt["data"]["objects"]) == {"ST02", "HS01", "NET"}
    assert any("ST99" in w for w in rt["warnings"])
    st = rt["data"]["objects"]["ST02"]
    assert {"ts2", "tin", "valve"} <= set(st)
    t = ts["data"]["stats"]["tin"]
    assert t["min"] <= t["mean"] <= t["max"] and ts["data"]["points"] == 48
    assert ts["data_ref"].startswith("/api/history/ST02")
    assert any("nope" in w for w in ts["warnings"])
    # 北苑小区站（ST07，属 HS02）注入了泄漏故障
    assert any(a["object_id"] == "ST07" and a["code"] == "MAKEUP" for a in al["data"]["alarms"])


# ---------------------------------------------------------------- 分析与预测
def test_analysis_tools(tc):
    async def s(c):
        an = await call(c, "analyze_anomaly_detect")
        eb = await call(c, "analyze_energy_benchmark", days=7, top_n=3)
        fc = await call(c, "predict_heat_load", scope_id="HS01", horizon_hours=48, granularity="hour")
        ef = await call(c, "analyze_control_effect", station_id="ST01")
        return an, eb, fc, ef

    an, eb, fc, ef = run(tc, s)
    found = {e["station_id"]: e["feature"] for e in an["data"]["events"]}
    assert found.get("ST03") == "heat_balance" and found.get("ST07") == "makeup"
    assert len(eb["data"]["worst"]) == 3 and eb["data"]["network"]["norm_heat"] > 0
    assert eb["data_ref"] and eb["evidence"]
    pts = fc["data"]["points"]
    assert pts and all(p["p10"] <= p["p50"] <= p["p90"] for p in pts)
    assert fc["confidence"] is not None
    assert ef["ok"] and ef["data"]["did_pct"] < 0


# ---------------------------------------------------------------- 调控链路（核心安全属性）
def test_ai_curve_flow_requires_human_approval(tc):
    async def s(c):
        opt = await call(c, "optimize_station_curve", station_id="ST02")
        plan = opt["data"]["plan_ref"]
        dr = await call(c, "control_draft_command", plan_ref=plan, reason="室温偏高，按 AI 曲线下调")
        did = dr["data"]["draft_id"]
        # 校验前不能提交审批
        early = await call(c, "control_request_approval", draft_id=did)
        chk = await call(c, "control_safety_check", draft_id=did)
        req = await call(c, "control_request_approval", draft_id=did, reason="请审批")
        forged = await call(c, "control_execute", draft_id=did, approval_token=f"{did}.9999999999.deadbeef")
        # —— 值班调度在平台上批准（人机界面接口，MCP 中没有对应工具）——
        tc.post(f"/api/control/drafts/{did}/approve", json={"approver": "张调度", "comment": "同意"})
        req2 = await call(c, "control_request_approval", draft_id=did)
        token = req2["data"]["approval_token"]
        ex = await call(c, "control_execute", draft_id=did, approval_token=token)
        reuse = await call(c, "control_execute", draft_id=did, approval_token=token)
        cid = ex["data"]["command_id"]
        mon = await call(c, "control_monitor_effect", command_id=cid)
        eff = await call(c, "analyze_control_effect", command_id=cid)
        rb = await call(c, "control_rollback", command_id=cid, reason="测试回滚")
        return opt, dr, early, chk, req, forged, req2, ex, reuse, mon, eff, rb

    opt, dr, early, chk, req, forged, req2, ex, reuse, mon, eff, rb = run(tc, s)
    assert opt["data"]["saving_pct"] > 0 and opt["data"]["plan_ref"] == "curve_opt:ST02:20"
    # 草案曲线来自服务端方案，而不是模型抄写
    assert dr["data"]["value"] == opt["data"]["recommended_curve"]
    assert not early["ok"] and early["error"]["code"] == "NOT_CHECKED"
    assert chk["data"]["verdict"] in ("pass", "warn") and "approval_token" not in chk["data"]  # ST02 是 L2
    assert req["data"]["status"] == "pending_approval" and req["data"]["approval_id"].startswith("AP")
    assert "approval_token" not in req["data"]
    assert not forged["ok"] and forged["error"]["code"] == "TOKEN_INVALID"
    assert req2["data"]["status"] == "approved" and req2["data"]["approver"] == "张调度"
    assert ex["ok"] and ex["data"]["command_id"].startswith("CMD")
    assert not reuse["ok"]                                   # 令牌一次性
    assert "tracking" in mon["data"]
    assert not eff["ok"] and "12 小时" in eff["error"]["message"]   # 刚下发的指令不做 DiD
    assert rb["data"]["status"] == "rolled_back"

    audit = tc.get("/api/audit").json()["data"]
    calls = [a for a in audit if a["action"] == "tool_call"]
    assert {"control_execute", "control_request_approval", "optimize_station_curve"} <= {a["detail"]["tool"] for a in calls}
    assert all("approval_token" not in a["detail"]["args"] for a in calls)     # 令牌不落审计
    assert any(a["user"].startswith("调度智能体") for a in audit if a["action"] == "request_approval")


def test_l3_station_auto_sign(tc):
    async def s(c):
        pol = await call(c, "control_get_autonomy_policy", station_id="ST01")
        curve = [[t, y - 0.5] for t, y in pol["data"]["current_settings"]["curve"]]
        dr = await call(c, "control_draft_command", station_id="ST01", param="climate_curve", value=curve, reason="微调")
        chk = await call(c, "control_safety_check", draft_id=dr["data"]["draft_id"])
        ex = await call(c, "control_execute", draft_id=dr["data"]["draft_id"], approval_token=chk["data"]["approval_token"])
        rb = await call(c, "control_rollback", command_id=ex["data"]["command_id"], reason="测试结束")
        return pol, chk, ex, rb

    pol, chk, ex, rb = run(tc, s)
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

    chk, req, bad = run(tc, s)
    assert chk["data"]["verdict"] == "block"
    assert not req["ok"] and req["error"]["code"] == "BLOCKED"
    assert not bad["ok"] and bad["error"]["code"] == "BAD_ARGUMENT"
