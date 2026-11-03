"""身份认证、权限、数据范围与委托令牌测试。"""
import json
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.auth import AGENT_PROFILES, ROLES
from app.main import S, app


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture(scope="module")
def tc():
    with TestClient(app) as c:
        yield c


def login(tc, u, pw):
    r = tc.post("/api/auth/login", json={"username": u, "password": pw})
    assert r.status_code == 200, r.text
    return r.json()["data"]["token"]


def test_no_agent_profile_can_approve():
    assert all(not perms & {"control:approve", "kb:review", "kb:manage"} for _, perms in AGENT_PROFILES.values())
    assert "control:approve" not in ROLES["dispatcher"][1] and "control:approve" in ROLES["chief"][1]


def test_unauthenticated_and_bad_tokens(tc):
    assert tc.get("/api/realtime").status_code == 401
    assert tc.get("/api/health").status_code == 200
    assert tc.get("/api/auth/demo-accounts").status_code == 200
    tok = login(tc, "manager", "mgr123")
    body, sig = tok.split(".")
    forged = body[:-2] + ("AA" if body[-2:] != "AA" else "BB") + "." + sig
    r = tc.get("/api/realtime", headers=H(forged))
    assert r.status_code == 401 and r.json()["error"]["code"] == "INVALID_TOKEN"
    expired = S.auth._sign({"sub": "manager", "kind": "human", "jti": "x", "exp": int(time.time()) - 1})
    r = tc.get("/api/realtime", headers=H(expired))
    assert r.status_code == 401 and r.json()["error"]["code"] == "TOKEN_EXPIRED"
    # 签名正确但从未登记的委托令牌（例如重启后）
    ghost = S.auth._sign({"sub": "chief", "kind": "agent", "agent": "x", "profile": "dispatch", "jti": "ghost",
                          "exp": int(time.time()) + 60})
    assert tc.get("/api/realtime", headers=H(ghost)).json()["error"]["code"] == "TOKEN_REVOKED"


def test_logout_revokes_session(tc):
    tok = login(tc, "service", "svc123")
    assert tc.get("/api/auth/me", headers=H(tok)).status_code == 200
    tc.post("/api/auth/logout", headers=H(tok))
    assert tc.get("/api/auth/me", headers=H(tok)).json()["error"]["code"] == "TOKEN_REVOKED"


def test_login_lockout(tc):
    for _ in range(5):
        assert tc.post("/api/auth/login", json={"username": "manager", "password": "bad"}).status_code == 401
    r = tc.post("/api/auth/login", json={"username": "manager", "password": "mgr123"})
    assert r.status_code == 429
    S.auth.login_failures.clear()


def test_role_permissions(tc):
    svc = login(tc, "service", "svc123")
    assert tc.get("/api/realtime", headers=H(svc)).status_code == 200
    for url in ("/api/analytics/energy", "/api/ai/diagnosis", "/api/audit", "/api/control/approvals"):
        assert tc.get(url, headers=H(svc)).status_code == 403, url
    mgr = login(tc, "manager", "mgr123")
    assert tc.get("/api/analytics/energy", headers=H(mgr)).status_code == 200
    r = tc.post("/api/control/drafts", headers=H(mgr), json={"station_id": "ST02", "param": "pump_frequency", "value": 45})
    assert r.status_code == 403 and "草拟调控指令" in r.json()["error"]["message"]


def test_data_scope(tc):
    z = login(tc, "zhao", "zhao123")
    me = tc.get("/api/auth/me", headers=H(z)).json()["data"]
    assert me["scope"]["sources"] == ["HS02"] and me["scope"]["stations"] == ["ST07", "ST08", "ST09", "ST10"]
    snap = tc.get("/api/realtime", headers=H(z)).json()["data"]
    assert set(snap["stations"]) == {"ST07", "ST08", "ST09", "ST10"} and set(snap["sources"]) == {"HS02"}
    assert all(a["object_id"] in ("HS02", "ST07", "ST08", "ST09", "ST10") for a in snap["alarms"])
    assert abs(snap["network"]["heat_mw"] - snap["sources"]["HS02"]["heat_mw"]) < 1e-6
    for url in ("/api/history/ST01", "/api/realtime/HS01", "/api/ai/curve/ST02", "/api/control/policy/ST05",
                "/api/analytics/daily?scope=HS01", "/api/ai/forecast?scope=ST03"):
        r = tc.get(url, headers=H(z))
        assert r.status_code == 403 and r.json()["error"]["code"] == "OUT_OF_SCOPE", url
    # NET = 数据范围内的汇总
    net = tc.get("/api/history/NET?hours=24&metrics=heat_mw", headers=H(z)).json()["data"]["heat_mw"]
    hs02 = tc.get("/api/history/HS02?hours=24&metrics=heat_mw", headers=H(z)).json()["data"]["heat_mw"]
    assert net == hs02
    en = tc.get("/api/analytics/energy?days=3", headers=H(z)).json()["data"]
    assert {r["id"] for r in en["stations"]} == {"ST07", "ST08", "ST09", "ST10"}
    assert en["network"]["name"].endswith("片区")
    md = tc.get("/api/analytics/multidim?metric=heat_gj&level=source", headers=H(z)).json()["data"]
    assert [r["id"] for r in md["rows"]] == ["HS02"]
    dg = tc.get("/api/ai/diagnosis", headers=H(z)).json()["data"]
    assert {s["station_id"] for s in dg["scores"]} == {"ST07", "ST08", "ST09", "ST10"}
    # 范围外报警不能确认
    out = next(a for a in S.engine.active_alarms() if a["object_id"] not in ("HS02", "ST07", "ST08", "ST09", "ST10"))
    assert tc.post(f"/api/alarms/{out['id']}/ack", headers=H(z)).status_code == 403


def test_websocket_auth(tc):
    with tc.websocket_connect("/ws/realtime?token=bad") as ws:
        assert ws.receive_json()["type"] == "auth_error"
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    z = login(tc, "zhao", "zhao123")
    with tc.websocket_connect(f"/ws/realtime?token={z}") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "snapshot" and set(msg["data"]["stations"]) == {"ST07", "ST08", "ST09", "ST10"}


def test_agent_tokens(tc):
    d = login(tc, "dispatcher", "disp123")
    r = tc.post("/api/auth/agent-tokens", headers=H(d), json={"agent": "调度智能体", "profile": "dispatch", "days": 500})
    rec = r.json()["data"]["record"]
    assert rec["permissions"] == sorted({"monitor:read", "analytics:read", "ai:read", "control:draft", "control:execute",
                                         "kb:read", "kb:write", "ops:read", "service:read", "report:read", "report:write"})
    agent = r.json()["data"]["token"]
    # 智能体不能再签发 / 管理委托令牌
    assert tc.post("/api/auth/agent-tokens", headers=H(agent), json={"profile": "analyst"}).status_code == 403
    assert tc.get("/api/auth/agent-tokens", headers=H(agent)).status_code == 403
    assert tc.post("/api/auth/agent-tokens", headers=H(d), json={"profile": "root"}).status_code == 400
    # 有效期被限制在 90 天内
    exp = json.loads(__import__("base64").urlsafe_b64decode(agent.split(".")[0] + "==="))["exp"]
    assert exp - time.time() <= 90 * 86400 + 5
    # 别人不能撤销我的令牌；令牌明文不入登记簿
    m = login(tc, "manager", "mgr123")
    assert tc.delete(f"/api/auth/agent-tokens/{rec['jti']}", headers=H(m)).status_code == 404
    assert "token" not in tc.get("/api/auth/agent-tokens", headers=H(d)).json()["data"][0]
    assert tc.delete(f"/api/auth/agent-tokens/{rec['jti']}", headers=H(d)).status_code == 200


def test_draft_token_only_to_owner(tc):
    d = login(tc, "dispatcher", "disp123")
    ch = login(tc, "chief", "chief123")
    dr = tc.post("/api/control/drafts", headers=H(d), json={"station_id": "ST02", "param": "pump_frequency",
                                                            "value": 44.5}).json()["data"]
    tc.post(f"/api/control/drafts/{dr['draft_id']}/check", headers=H(d))
    tc.post(f"/api/control/drafts/{dr['draft_id']}/request-approval", headers=H(d), json={"reason": "节电"})
    r = tc.post(f"/api/control/drafts/{dr['draft_id']}/approve", headers=H(ch), json={})
    assert r.json()["data"]["approval"]["token"] is None
    own = tc.get(f"/api/control/drafts/{dr['draft_id']}", headers=H(d)).json()["data"]
    assert own["approval"]["token"]
    cmd = tc.post(f"/api/control/drafts/{dr['draft_id']}/execute", headers=H(d),
                  json={"token": own["approval"]["token"]}).json()["data"]
    assert cmd["executed_by"] == "张调度（调度员）" and cmd["approver"] == "李调度长（值班调度长）"
    tc.post(f"/api/control/commands/{cmd['command_id']}/rollback", headers=H(d), json={"reason": "测试"})
