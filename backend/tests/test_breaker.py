"""熔断测试：单元（可控时钟）+ 接口集成（真实回滚触发熔断）+ MCP。"""
import asyncio

import pytest
from fastapi.testclient import TestClient
from mcp import Client

from app.main import S, app
from app.services.breaker import BreakerConfig, BreakerError, CircuitBreaker
from mcp_server import HeatnetAPI, build_server

HS01_STATIONS = ["ST01", "ST02", "ST03", "ST04", "ST05", "ST06"]


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t

    def advance(self, minutes):
        self.t += minutes * 60


def make():
    clk = Clock()
    cfg = BreakerConfig(rollback_limit=3, window_s=3600, missing_limit=0.2, min_coverage_s=600, reset_window_s=600)
    return CircuitBreaker(cfg, clk), clk


def feed(b, clk, minutes, offline: set[str], step_s=60):
    """按分钟喂数据样本：offline 中的站视为通讯中断。"""
    for _ in range(int(minutes * 60 / step_s)):
        clk.t += step_s
        b.record_sample({s: s not in offline for s in HS01_STATIONS}, step_s)


# ---------------------------------------------------------------- 单元
def test_rollbacks_within_window_trip():
    b, clk = make()
    trips = []
    b.on_trip.append(lambda net, reasons: trips.append((net, reasons)))
    for i in range(2):
        b.record_rollback("ST01", f"C{i}")
        clk.advance(10)
    assert not b.is_tripped("ST02")
    b.record_rollback("ST03", "C3")
    assert b.is_tripped("ST05") and b.effective_autonomy("ST01") == "L1"       # 整个 HS01 管网
    assert not b.is_tripped("ST07") and b.effective_autonomy("ST07") == "L2"   # 其他管网不受影响
    assert trips[0][0] == "HS01" and "回滚 3 次" in trips[0][1][0]


def test_rollbacks_outside_window_do_not_trip():
    b, clk = make()
    for i in range(3):
        b.record_rollback("ST01", f"C{i}")
        clk.advance(31)                               # 间隔 31 分钟：任一 60 分钟窗口内最多 2 次
    assert not b.is_tripped("ST01")
    assert b.metrics("HS01")["rollbacks"] == 1


def test_missing_rate_needs_coverage_and_threshold():
    b, clk = make()
    feed(b, clk, 5, {"ST01", "ST02", "ST03"})        # 50% 缺失但只覆盖 5 分钟：不判
    assert not b.is_tripped("ST01")
    feed(b, clk, 6, {"ST01", "ST02", "ST03"})        # 覆盖满 10 分钟后：50% > 20% 熔断
    assert b.is_tripped("ST01")
    assert "数据缺失率" in b.nets["HS01"].reasons[0]

    b2, clk2 = make()
    feed(b2, clk2, 30, {"ST01"})                     # 1/6 ≈ 17% 持续半小时：未超阈值
    assert not b2.is_tripped("ST01")


def test_reset_rules():
    b, clk = make()
    feed(b, clk, 11, {"ST01", "ST02", "ST03"})
    assert b.is_tripped("ST01")
    with pytest.raises(BreakerError, match="复核结论"):
        b.reset("HS01", "李调度长", "")
    with pytest.raises(BreakerError) as e:            # 数据仍然缺失：不允许解除
        b.reset("HS01", "李调度长", "已检查")
    assert e.value.code == "CONDITION_PERSISTS"
    feed(b, clk, 11, set())                           # 通讯恢复 10 分钟以上
    b.reset("HS01", "李调度长", "通讯已恢复")
    assert not b.is_tripped("ST01")
    assert b.nets["HS01"].events[0]["action"] == "reset"


def test_reset_clears_rollback_baseline():
    b, clk = make()
    for i in range(3):
        b.record_rollback("ST01", f"C{i}")
    assert b.is_tripped("ST01")
    b.reset("HS01", "李调度长", "原因已查明：曲线方案有误")
    b.record_rollback("ST01", "C4")                   # 窗口内仍有 3 次旧回滚，但复核后不再计入
    assert not b.is_tripped("ST01") and b.metrics("HS01")["rollbacks"] == 1


def test_manual_trip():
    b, _ = make()
    b.trip("HS02", "李调度长", "城北片区管网检修")
    assert b.is_tripped("ST09") and b.nets["HS02"].manual
    with pytest.raises(BreakerError):
        b.trip("HS02", "李调度长", "重复")
    with pytest.raises(BreakerError):
        b.reset("HS01", "李调度长", "未熔断")


# ---------------------------------------------------------------- 接口集成
def H(tok):
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture(scope="module")
def tc():
    with TestClient(app) as c:
        c.s = {u: c.post("/api/auth/login", json={"username": u, "password": pw}).json()["data"]["token"]
               for u, pw in [("dispatcher", "disp123"), ("chief", "chief123"), ("zhao", "zhao123")]}
        c.agent = c.post("/api/auth/agent-tokens", headers=H(c.s["dispatcher"]),
                         json={"profile": "dispatch", "days": 1}).json()["data"]["token"]
        yield c


def draft(tc, tok, sid, param, value):
    r = tc.post("/api/control/drafts", headers=H(tok), json={"station_id": sid, "param": param, "value": value,
                                                             "reason": "测试"})
    assert r.status_code == 200, r.text
    return r.json()["data"]["draft_id"]


def curve_minus(tc, sid, d):
    cur = tc.get(f"/api/control/settings/{sid}", headers=H(tc.s["dispatcher"])).json()["data"]["curve"]
    return [[t, y + d] for t, y in cur]


def test_rollbacks_trip_network_and_enforce_l1(tc):
    disp, chief, agent = tc.s["dispatcher"], tc.s["chief"], tc.agent

    # 熔断前：ST03 已批准待执行的人工草案；ST02 智能体的待审批请求
    d_old = draft(tc, disp, "ST03", "pump_frequency", 44)
    tc.post(f"/api/control/drafts/{d_old}/check", headers=H(disp))
    tc.post(f"/api/control/drafts/{d_old}/request-approval", headers=H(disp), json={})
    tc.post(f"/api/control/drafts/{d_old}/approve", headers=H(chief), json={"comment": "同意"})
    old_token = tc.get(f"/api/control/drafts/{d_old}", headers=H(disp)).json()["data"]["approval"]["token"]
    d_agent = draft(tc, agent, "ST02", "pump_frequency", 45)
    tc.post(f"/api/control/drafts/{d_agent}/check", headers=H(agent))
    assert tc.post(f"/api/control/drafts/{d_agent}/request-approval", headers=H(agent),
                   json={}).json()["data"]["status"] == "pending_approval"

    # 锦绣花园站（L3）连续 3 次 下发→回滚
    for i in range(3):
        did = draft(tc, disp, "ST01", "climate_curve", curve_minus(tc, "ST01", -0.5))
        chk = tc.post(f"/api/control/drafts/{did}/check", headers=H(disp)).json()["data"]
        assert chk["status"] == "approved"                       # L3 自动签发
        cmd = tc.post(f"/api/control/drafts/{did}/execute", headers=H(disp),
                      json={"token": chk["approval"]["token"]}).json()["data"]
        tc.post(f"/api/control/commands/{cmd['command_id']}/rollback", headers=H(disp), json={"reason": f"效果不佳 {i}"})

    st = {r["network_id"]: r for r in tc.get("/api/breaker", headers=H(chief)).json()["data"]}
    assert st["HS01"]["state"] == "tripped" and "回滚 3 次" in st["HS01"]["reasons"][0]
    assert st["HS02"]["state"] == "normal"
    assert all(s["effective"] == "L1" for s in st["HS01"]["stations"])

    pol = tc.get("/api/control/policy/ST01", headers=H(disp)).json()["data"]
    assert pol["autonomy"] == "L1" and pol["configured_autonomy"] == "L3" and not pol["auto_sign"]
    # L3 不再自动签发
    did = draft(tc, disp, "ST01", "climate_curve", curve_minus(tc, "ST01", -0.5))
    assert tc.post(f"/api/control/drafts/{did}/check", headers=H(disp)).json()["data"]["status"] == "checked"
    # 熔断前签发的令牌作废
    r = tc.post(f"/api/control/drafts/{d_old}/execute", headers=H(disp), json={"token": old_token})
    assert r.status_code == 400 and r.json()["error"]["code"] == "TOKEN_REVOKED"
    # 智能体的待审批请求被取消
    assert tc.get(f"/api/control/drafts/{d_agent}", headers=H(disp)).json()["data"]["status"] == "cancelled"
    # 智能体：R1 草拟/校验可以，R2 提交审批不行
    d2 = draft(tc, agent, "ST02", "pump_frequency", 45.5)
    assert tc.post(f"/api/control/drafts/{d2}/check", headers=H(agent)).status_code == 200
    r = tc.post(f"/api/control/drafts/{d2}/request-approval", headers=H(agent), json={})
    assert r.status_code == 400 and r.json()["error"]["code"] == "BREAKER_OPEN"
    # 其他管网的智能体操作不受影响
    d3 = draft(tc, agent, "ST08", "pump_frequency", 43.5)
    tc.post(f"/api/control/drafts/{d3}/check", headers=H(agent))
    assert tc.post(f"/api/control/drafts/{d3}/request-approval", headers=H(agent), json={}).status_code == 200
    # 人工操作照常：调度员提交 → 调度长批准 → 调度员下发
    d4 = draft(tc, disp, "ST02", "pump_frequency", 45.5)
    tc.post(f"/api/control/drafts/{d4}/check", headers=H(disp))
    tc.post(f"/api/control/drafts/{d4}/request-approval", headers=H(disp), json={})
    tc.post(f"/api/control/drafts/{d4}/approve", headers=H(chief), json={})
    tok4 = tc.get(f"/api/control/drafts/{d4}", headers=H(disp)).json()["data"]["approval"]["token"]
    assert tc.post(f"/api/control/drafts/{d4}/execute", headers=H(disp), json={"token": tok4}).status_code == 200

    # 一级报警 + 审计
    S.engine.tick(0.1)
    alarm = S.engine.alarms.get("HS01-BREAKER")
    assert alarm and alarm["level"] == 1
    audit = tc.get("/api/audit", headers=H(chief)).json()["data"]
    trip = next(a for a in audit if a["action"] == "breaker_trip")
    assert d_old in trip["detail"]["revoked_tokens"] and d_agent in trip["detail"]["cancelled_requests"]

    # 片区站长只看到自己片区的熔断状态
    assert [r["network_id"] for r in tc.get("/api/breaker", headers=H(tc.s["zhao"])).json()["data"]] == ["HS02"]

    # 解除：调度员无权；调度长须填复核结论
    assert tc.post("/api/breaker/HS01/reset", headers=H(disp), json={"comment": "x"}).status_code == 403
    assert tc.post("/api/breaker/HS01/reset", headers=H(agent), json={"comment": "x"}).status_code == 403
    r = tc.post("/api/breaker/HS01/reset", headers=H(chief), json={"comment": ""})
    assert r.json()["error"]["code"] == "COMMENT_REQUIRED"
    assert tc.post("/api/breaker/HS01/reset", headers=H(chief),
                   json={"comment": "已核查：曲线下调方案过激，改为小步调整"}).status_code == 200
    S.engine.tick(0.1)
    assert "HS01-BREAKER" not in S.engine.alarms
    did = draft(tc, disp, "ST01", "climate_curve", curve_minus(tc, "ST01", -0.5))
    assert tc.post(f"/api/control/drafts/{did}/check", headers=H(disp)).json()["data"]["status"] == "approved"
    assert any(a["action"] == "breaker_reset" for a in tc.get("/api/audit", headers=H(chief)).json()["data"])


def test_manual_trip_and_mcp_view(tc):
    chief = tc.s["chief"]
    assert tc.post("/api/breaker/HS02/trip", headers=H(chief), json={"comment": "管网检修"}).status_code == 200

    async def scn(c):
        pol = (await c.call_tool("control_get_autonomy_policy", {"station_id": "ST09"})).structured_content
        dr = (await c.call_tool("control_draft_command", {"station_id": "ST09", "param": "pump_frequency",
                                                           "value": 44, "reason": "测试"})).structured_content
        await c.call_tool("control_safety_check", {"draft_id": dr["data"]["draft_id"]})
        req = (await c.call_tool("control_request_approval", {"draft_id": dr["data"]["draft_id"]})).structured_content
        return pol, dr, req

    async def main():
        async with Client(build_server(HeatnetAPI(client=tc, token=tc.agent))) as c:
            return await scn(c)

    pol, dr, req = asyncio.run(main())
    assert pol["data"]["policy"]["autonomy"] == "L1" and pol["data"]["policy"]["breaker"]["state"] == "tripped"
    assert "熔断" in pol["summary"]
    assert dr["ok"]                                                   # R1 仍可用
    assert not req["ok"] and req["error"]["code"] == "BREAKER_OPEN"
    tc.post("/api/breaker/HS02/reset", headers=H(chief), json={"comment": "检修结束"})
