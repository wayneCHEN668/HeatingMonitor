"""viz_render：data_ref 白名单与按查看者解析、规格校验、范围与权限、工作台、MCP。"""
import asyncio

import pytest
from fastapi.testclient import TestClient
from mcp import Client

from app.main import S, app
from app.services.viz import VizService
from mcp_server import HeatnetAPI, build_server


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


def login(tc, user, pw):
    return tc.post("/api/auth/login", json={"username": user, "password": pw}).json()["data"]["token"]


@pytest.fixture(scope="module")
def tc():
    with TestClient(app) as c:
        c.s = {u: login(c, u, pw) for u, pw in [("dispatcher", "disp123"), ("zhao", "zhao123"), ("service", "svc123")]}
        c.agent = {}
        for who, prof in (("dispatcher", "dispatch"), ("zhao", "dispatch")):
            r = c.post("/api/auth/agent-tokens", headers=H(c.s[who]), json={"agent": "可视化测试", "profile": prof, "days": 7})
            c.agent[who] = r.json()["data"]["token"]
        yield c


def make(tc, who, mode, spec, title="测试", token=None):
    return tc.post("/api/viz", headers=H(token or tc.s[who]), json={"mode": mode, "title": title, "spec": spec})


def chart(*series, **kw):
    return {"type": "line", "series": [{"data_ref": r, "field": f} for r, f in series], **kw}


# ---------------------------------------------------------------- 图表
def test_chart_renders_viewer_data_and_aligns_series(tc):
    r = make(tc, "dispatcher", "chart", chart(("/api/history/ST02?hours=24&metrics=tin,ts2", "tin"),
                                              ("/api/history/ST03?hours=12&metrics=tin", "tin"),
                                              mark_lines=[{"value": 18, "label": "室温下限"}]))
    assert r.status_code == 200, r.text
    vid = r.json()["data"]["viz_id"]
    d = tc.get(f"/api/viz/{vid}", headers=H(tc.s["dispatcher"])).json()["data"]
    assert d["x_kind"] == "time" and len(d["x"]) == 24
    a, b = d["series_data"]
    assert len(a["data"]) == len(b["data"]) == 24
    assert b["data"][:12] == [None] * 12 and None not in b["data"][12:]          # 短序列按标签对齐，缺失补空
    df = S.store.frame("ST02").iloc[-24:]
    assert a["data"][-1] == pytest.approx(float(df["tin"].iloc[-1]), abs=1e-3)
    assert d["mark_lines"][0]["value"] == 18


def test_category_and_forecast_refs(tc):
    r = make(tc, "dispatcher", "chart", {"type": "bar", "series": [{"data_ref": "/api/realtime", "field": "tin"}]})
    d = tc.get(f"/api/viz/{r.json()['data']['viz_id']}", headers=H(tc.s["dispatcher"])).json()["data"]
    assert d["x_kind"] == "category" and len(d["x"]) == 13
    r = make(tc, "dispatcher", "chart", chart(("/api/ai/forecast?scope=NET&horizon=24", "actual"),
                                              ("/api/ai/forecast?scope=NET&horizon=24", "p50")))
    d = tc.get(f"/api/viz/{r.json()['data']['viz_id']}", headers=H(tc.s["dispatcher"])).json()["data"]
    actual, p50 = d["series_data"]
    assert actual["data"][-1] is None and p50["data"][0] is None and p50["data"][-1] is not None


@pytest.mark.parametrize("spec,code", [
    (chart(("https://evil.example/x", "tin")), "BAD_REF"),
    (chart(("/api/service/customers/lookup?key=C01011011&reveal=true", "phone")), "BAD_REF"),   # 不在白名单
    (chart(("/api/history/ST02?hours=99999", "tin")), "BAD_REF"),
    (chart(("/api/history/ST02?metrics=nosuch", "nosuch")), "BAD_REF"),
    (chart(("/api/history/ST02", "p2s")), "BAD_SPEC"),                                          # 字段不在数据集里
    ({"type": "line", "series": [{"data_ref": "/api/realtime", "field": "tin", "data": [1, 2, 3]}]}, "BAD_SPEC"),  # 内联数据
    ({"type": "line", "option": {"tooltip": {"formatter": "<img src=x onerror=alert(1)>"}},
      "series": [{"data_ref": "/api/realtime", "field": "tin"}]}, "BAD_SPEC"),                  # 原始 ECharts option
    ({"type": "pie", "series": [{"data_ref": "/api/realtime", "field": "tin"}]}, "BAD_SPEC"),
    (chart(("/api/realtime", "tin"), ("/api/history/ST02", "tin")), "BAD_SPEC"),                 # 分类与时间混用
])
def test_spec_rejections(tc, spec, code):
    r = make(tc, "dispatcher", "chart", spec)
    assert r.status_code == 400 and r.json()["error"]["code"] == code, r.text


def test_text_is_sanitized(tc):
    r = make(tc, "dispatcher", "map", {"object_ids": ["ST07"], "note": "<b>补水</b>异常"}, title="<script>x</script>定位")
    d = tc.get(f"/api/viz/{r.json()['data']['viz_id']}", headers=H(tc.s["dispatcher"])).json()["data"]
    assert "<" not in d["title"] and "<" not in d["note"]


# ---------------------------------------------------------------- 范围与权限
def test_scope_permissions_and_ownership(tc):
    z = tc.s["zhao"]
    assert make(tc, "zhao", "chart", chart(("/api/history/ST01", "tin"))).status_code == 403
    assert make(tc, "zhao", "map", {"object_ids": ["ST08", "ST01"]}).status_code == 403
    assert make(tc, "zhao", "twin", {"object_id": "HS01", "parts": ["units"]}).status_code == 403
    # 片区用户的 /api/realtime 只含本片区的站
    r = make(tc, "zhao", "chart", {"type": "bar", "series": [{"data_ref": "/api/realtime", "field": "tin"}]})
    d = tc.get(f"/api/viz/{r.json()['data']['viz_id']}", headers=H(z)).json()["data"]
    assert len(d["x"]) == 4
    # 客服坐席没有 ai:read，不能画负荷预测
    r = make(tc, "service", "chart", chart(("/api/ai/forecast?scope=NET", "p50")))
    assert r.status_code == 403
    # 可视化属于本人：别人打不开
    vid = make(tc, "dispatcher", "map", {"object_ids": ["ST07"]}).json()["data"]["viz_id"]
    assert tc.get(f"/api/viz/{vid}", headers=H(z)).status_code == 404


def test_twin_parts_validation(tc):
    assert make(tc, "dispatcher", "twin", {"object_id": "ST07", "parts": ["makeup", "valve"]}).status_code == 200
    r = make(tc, "dispatcher", "twin", {"object_id": "ST07", "parts": ["units"]})          # 热源部件不适用于热力站
    assert r.status_code == 400 and "makeup" in r.json()["error"]["message"]


# ---------------------------------------------------------------- 工作台
def test_workbench_pin_and_persistence(tc):
    agent = tc.agent["dispatcher"]
    r = make(tc, None, "chart", chart(("/api/history/ST05?hours=48&metrics=heat_mw", "heat_mw")), token=agent)
    vid = r.json()["data"]["viz_id"]
    # 智能体生成的可视化归委托人所有
    assert tc.get(f"/api/viz/{vid}", headers=H(tc.s["dispatcher"])).status_code == 200
    r = tc.post(f"/api/viz/{vid}/pin", headers=H(agent), json={"pinned": True})
    assert r.status_code == 403 and r.json()["error"]["code"] == "HUMAN_REQUIRED"
    assert tc.post(f"/api/viz/{vid}/pin", headers=H(tc.s["dispatcher"]), json={"pinned": True}).json()["data"]["pinned"]
    rows = tc.get("/api/viz", headers=H(tc.s["dispatcher"])).json()["data"]
    assert rows[0]["viz_id"] == vid and rows[0]["pinned"]
    fresh = VizService(S.store, S.ai, S.analytics)
    assert fresh.items[vid]["pinned"]
    assert tc.delete(f"/api/viz/{vid}", headers=H(tc.s["dispatcher"])).status_code == 200


# ---------------------------------------------------------------- MCP
def test_mcp_viz_render_uses_data_ref(tc):
    async def s(c):
        summ = (await c.call_tool("data_get_timeseries_summary", {"object_id": "ST07", "metrics": ["water_t", "tin"], "hours": 48})).structured_content
        ch = (await c.call_tool("viz_render", {"mode": "chart", "title": "北苑补水", "data_ref": summ["data_ref"],
                                               "fields": ["water_t", "tin"], "right_axis_fields": ["tin"]})).structured_content
        mp = (await c.call_tool("viz_render", {"mode": "map", "object_ids": ["ST07"], "color_by": "makeup_tph",
                                               "note": "补水异常", "level": "danger"})).structured_content
        tw = (await c.call_tool("viz_render", {"mode": "twin", "object_ids": ["ST07"], "parts": ["makeup"]})).structured_content
        bad = (await c.call_tool("viz_render", {"mode": "map", "object_ids": ["ST01"]})).structured_content
        return summ, ch, mp, tw, bad

    async def run(token, scen):
        async with Client(build_server(HeatnetAPI(client=tc, token=token))) as c:
            return await scen(c)

    summ, ch, mp, tw, bad = asyncio.run(run(tc.agent["dispatcher"], s))
    assert summ["data_ref"].startswith("/api/history/ST07")
    assert ch["ok"] and ch["data"]["link"].startswith("/viz/") and "water_t" not in str(ch["data"].get("values", ""))
    d = tc.get(ch["data"]["link"].replace("/viz/", "/api/viz/"), headers=H(tc.s["dispatcher"])).json()["data"]
    assert [s["axis"] for s in d["series_data"]] == ["left", "right"]
    assert mp["ok"] and tw["ok"] and "补水系统" in tw["data"]["description"]
    zbad = asyncio.run(run(tc.agent["zhao"], lambda c: c.call_tool("viz_render", {"mode": "map", "object_ids": ["ST01"]})))
    assert not zbad.structured_content["ok"] and zbad.structured_content["error"]["code"] == "OUT_OF_SCOPE"
    assert bad["ok"]                                                           # 调度员全网可见
