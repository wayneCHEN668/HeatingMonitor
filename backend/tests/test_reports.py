"""报告生成：数字核对、口径一致性、权限与范围、事故报告审核、HTML 转义、持久化、MCP。"""
import asyncio

import pytest
from fastapi.testclient import TestClient
from mcp import Client

from app.main import S, app
from app.services.reports import Facts, ReportService
from mcp_server import HeatnetAPI, build_server


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


def login(tc, user, pw):
    return tc.post("/api/auth/login", json={"username": user, "password": pw}).json()["data"]["token"]


@pytest.fixture(scope="module")
def tc():
    with TestClient(app) as c:
        c.s = {u: login(c, u, pw) for u, pw in [("chief", "chief123"), ("dispatcher", "disp123"), ("manager", "mgr123"),
                                                ("zhao", "zhao123"), ("service", "svc123")]}
        r = c.post("/api/auth/agent-tokens", headers=H(c.s["manager"]), json={"agent": "经营分析智能体", "profile": "analyst", "days": 7})
        c.analyst = r.json()["data"]["token"]
        yield c


def gen(tc, who, **body):
    return tc.post("/api/reports", headers=H(tc.s[who]), json=body)


# ---------------------------------------------------------------- 数字核对
def test_fact_check_unit():
    F = Facts()
    t = f"供热量 {F.n('heat', 1234.56, 1)} GJ，{F.lit('北站家园2号楼')}，{F.lit('2026-11-02')}"
    assert F.verify([t])["ok"]
    assert not F.verify([t + "，节约 99 GJ"])["ok"]                        # 未登记的数字
    F.n("five", 5, 0)
    assert not F.verify(["共 55 户"])["ok"]                                 # 5 已登记不代表 55 可用
    assert F.verify(["共 5 户"])["ok"]
    assert F.n("neg", -0.04, 1) == "0.0"                                    # 不出现 -0.0


def test_daily_matches_analytics_and_reverifies(tc):
    r = gen(tc, "chief", report_type="daily").json()
    assert r["ok"] and r["data"]["numbers_checked"] > 20
    rep = S.reports.get(r["data"]["report_id"])
    day = rep["period"]["label"]
    ref = S.analytics.daily(__import__("datetime").date.fromisoformat(day), "NET", None)["summary"]
    assert rep["facts"]["heat_gj"]["value"] == pytest.approx(ref["heat_gj"])
    assert ReportService.reverify(rep)["ok"]
    # 篡改正文中的数字后复核失败
    bad = {**rep, "sections": [dict(s) for s in rep["sections"]]}
    bad["sections"][0]["paragraphs"] = [bad["sections"][0]["paragraphs"][0].replace(rep["facts"]["heat_gj"]["text"], "9999.9")]
    v = ReportService.reverify(bad)
    assert not v["ok"] and v["unmatched"][0]["number"] == "9999.9"


def test_energy_decomposition_and_consistency(tc):
    period = "2026-10-27~2026-11-02"
    e = S.reports.get(gen(tc, "chief", report_type="energy", period=period).json()["data"]["report_id"])
    f = {k: v["value"] for k, v in e["facts"].items()}
    assert f["weather_eff"] + f["intensity_eff"] == pytest.approx(f["dq"], abs=1e-6)     # 分解恒等式
    assert f["saved"] == pytest.approx(-f["intensity_eff"], abs=1e-6)
    expected = f["heat_base"] * f["hdd"] / f["hdd_base"]
    assert f["rate"] == pytest.approx(f["saved"] / expected * 100, abs=1e-6)
    w = S.reports.get(gen(tc, "chief", report_type="weekly", period=period).json()["data"]["report_id"])
    assert w["facts"]["norm_yoy"]["text"] == e["facts"]["rate"]["text"]               # 周报同比与节能率口径一致


def test_period_validation(tc):
    assert gen(tc, "chief", report_type="daily", period="2026/11/02").status_code == 400
    assert gen(tc, "chief", report_type="energy", period="2026-08-01~2026-11-02").status_code == 400
    assert gen(tc, "chief", report_type="weekly", period="2099-01-07").status_code == 400
    assert gen(tc, "chief", report_type="monthly").status_code == 400


def test_dedupe_and_force(tc):
    a = gen(tc, "dispatcher", report_type="daily", scope_id="HS01").json()["data"]
    b = gen(tc, "dispatcher", report_type="daily", scope_id="HS01").json()["data"]
    c = gen(tc, "dispatcher", report_type="daily", scope_id="HS01", force=True).json()["data"]
    assert b["reused"] and b["report_id"] == a["report_id"] and c["report_id"] != a["report_id"]


# ---------------------------------------------------------------- 权限与范围
def test_scope_and_visibility(tc):
    z = gen(tc, "zhao", report_type="daily").json()["data"]
    assert "城北燃气调峰锅炉房" in z["scope_name"]
    assert gen(tc, "zhao", report_type="daily", scope_id="ST01").status_code == 403
    g = gen(tc, "chief", report_type="weekly").json()["data"]
    assert tc.get(f"/api/reports/{g['report_id']}", headers=H(tc.s["zhao"])).status_code == 403
    ids = {r["report_id"] for r in tc.get("/api/reports", headers=H(tc.s["zhao"])).json()["data"]}
    assert z["report_id"] in ids and g["report_id"] not in ids
    assert tc.get(f"/api/reports/{z['report_id']}", headers=H(tc.s["chief"])).status_code == 200
    # 客服坐席没有报告权限
    assert tc.get("/api/reports", headers=H(tc.s["service"])).status_code == 403


def test_sections_follow_caller_permissions(tc):
    """分析智能体（manager 委托）没有 ops:read：周报里不出现工单章节；manager 本人生成则有。"""
    async def s(c):
        r = await c.call_tool("doc_generate_report", {"report_type": "weekly", "period": "2026-10-26~2026-11-01"})
        return r.structured_content

    out = asyncio.run(_run(tc, tc.analyst, s))
    assert out["ok"] and out["data"]["link"].startswith("/reports/") and out["data"]["key_findings"]
    rep = S.reports.get(out["data"]["report_id"])
    assert rep["actor_kind"] == "agent" and not any("工单" in s["heading"] for s in rep["sections"])
    mine = S.reports.get(gen(tc, "manager", report_type="weekly", period="2026-10-26~2026-11-01").json()["data"]["report_id"])
    assert any("工单" in s["heading"] for s in mine["sections"])


async def _run(tc, token, scenario):
    async with Client(build_server(HeatnetAPI(client=tc, token=token))) as c:
        return await scenario(c)


# ---------------------------------------------------------------- 事故报告
def _command_with_rollback(tc):
    disp, chief = H(tc.s["dispatcher"]), H(tc.s["chief"])
    freq = tc.get("/api/control/settings/ST08", headers=disp).json()["data"]["freq"]
    d = tc.post("/api/control/drafts", headers=disp, json={"station_id": "ST08", "param": "pump_frequency",
                                                           "value": freq - 2, "reason": "降频节电"}).json()["data"]["draft_id"]
    tc.post(f"/api/control/drafts/{d}/check", headers=disp)
    tc.post(f"/api/control/drafts/{d}/request-approval", headers=disp, json={})
    tc.post(f"/api/control/drafts/{d}/approve", headers=chief, json={"comment": "同意"})
    token = tc.get(f"/api/control/drafts/{d}", headers=disp).json()["data"]["approval"]["token"]
    cmd = tc.post(f"/api/control/drafts/{d}/execute", headers=disp, json={"token": token}).json()["data"]
    tc.post(f"/api/control/commands/{cmd['command_id']}/rollback", headers=disp, json={"reason": "末端压差不足"})
    tc.post("/api/ops/workorders", headers=disp, json={"type": "设备故障", "object_id": "ST08",
                                                        "description": "<script>alert(1)</script> 循环泵异响"})
    return cmd["command_id"]


def test_incident_report_review_and_html(tc):
    cid = _command_with_rollback(tc)
    assert gen(tc, "dispatcher", report_type="incident", event_id=cid).status_code == 403     # 无审计权限
    r = gen(tc, "chief", report_type="incident", event_id=cid).json()
    assert r["ok"], r
    rep = S.reports.get(r["data"]["report_id"])
    assert rep["status"] == "草稿（待审核）"
    kinds = [x["kind"] for x in rep["sections"][1]["table"]["rows"]]
    assert {"草拟指令", "批准", "下发", "回滚"} <= set(kinds)
    assert kinds.index("下发") < kinds.index("回滚")
    # HTML：用户输入转义，带 CSP
    h = tc.get(f"/api/reports/{rep['report_id']}/html", headers=H(tc.s["chief"]))
    assert h.status_code == 200 and "<script>alert" not in h.text and "&lt;script&gt;" in h.text
    assert "default-src 'none'" in h.headers["content-security-policy"]
    # 审核：须填结论；只能人工；通过后结论附在末尾且标为人工填写
    rid = rep["report_id"]
    assert tc.post(f"/api/reports/{rid}/review", headers=H(tc.s["chief"]), json={"approve": True}).status_code == 400
    ok = tc.post(f"/api/reports/{rid}/review", headers=H(tc.s["chief"]),
                 json={"approve": True, "conclusion": "降频后末端压差不足，已回滚；今后降频先校核最不利环路。"}).json()
    assert ok["data"]["status"] == "已审核"
    rep = S.reports.get(rid)
    assert rep["sections"][-1]["human_written"] and ReportService.reverify(rep)["ok"]
    assert tc.post(f"/api/reports/{rid}/review", headers=H(tc.s["chief"]), json={"approve": True, "conclusion": "再次审核测试"}).status_code == 400


def test_persistence(tc):
    rid = gen(tc, "chief", report_type="daily", scope_id="ST02").json()["data"]["report_id"]
    fresh = ReportService(S.analytics, S.ai, S.engine, S.control, S.breaker, S.ops, S.service, S.kb)
    assert rid in fresh.reports and ReportService.reverify(fresh.reports[rid])["ok"]
