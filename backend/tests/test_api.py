import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.config import STATIONS
from app.main import app
from app.sim.physics import build_station_model


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def data(r):
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] is True and "trace_id" in j
    return j["data"]


# ---------------- 机理模型 ----------------
def test_physics_energy_balance():
    for cfg in STATIONS:
        m = build_station_model(cfg)
        r = m.solve(90.0, -5.0, 0.6)
        q1 = r["g1"] / 3.6 * 4.186 * (90.0 - r["tr1"]) / 1000
        q2 = r["g2"] / 3.6 * 4.186 * (r["ts2"] - r["tr2"]) / 1000
        assert abs(q1 - r["heat_mw"]) < 1e-6 and abs(q2 - r["heat_mw"]) < 1e-6
        assert r["ts2"] > r["tr2"] > r["tin"] > -5.0


def test_valve_inverse_hits_setpoint():
    m = build_station_model(STATIONS[0])
    v, sat = m.valve_for_ts2(np.array([85.0]), -3.0, 42.0)
    assert not sat[0]
    assert abs(m.solve(85.0, -3.0, v)["ts2"][0] - 42.0) < 0.05


# ---------------- 监控 ----------------
def test_meta_and_realtime(client):
    meta = data(client.get("/api/meta"))
    assert len(meta["sources"]) == 3 and len(meta["stations"]) == 13
    rt = data(client.get("/api/realtime"))
    assert set(rt["stations"]) == {s.id for s in STATIONS}
    assert rt["network"]["heat_mw"] > 0
    hs = data(client.get("/api/realtime/HS02"))
    assert any(u["type"] == "boiler" for u in hs["units"])
    h = data(client.get("/api/history/ST01?hours=24&metrics=ts2,tr2"))
    assert len(h["ts"]) == 24 and set(h) == {"ts", "ts2", "tr2"}


def test_websocket(client):
    with client.websocket_connect("/ws/realtime") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "snapshot" and "stations" in msg["data"]


# ---------------- 调控链路 ----------------
def test_control_block_large_step(client):
    cur = data(client.get("/api/control/settings/ST02"))
    d = data(client.post("/api/control/drafts", json={"station_id": "ST02", "param": "valve_opening",
                                                     "value": max(5, cur["valve_set"] - 40)}))
    chk = data(client.post(f"/api/control/drafts/{d['draft_id']}/check"))
    assert chk["check"]["verdict"] == "block"
    r = client.post(f"/api/control/drafts/{d['draft_id']}/approve", json={})
    assert r.status_code == 400 and r.json()["error"]["code"] == "BLOCKED"


def test_control_full_flow_and_rollback(client):
    cur = data(client.get("/api/control/settings/ST02"))
    target = round(cur["valve_set"] - 5, 1)
    d = data(client.post("/api/control/drafts", json={"station_id": "ST02", "param": "valve_opening", "value": target}))
    # 没有令牌不能执行
    r = client.post(f"/api/control/drafts/{d['draft_id']}/execute", json={"token": "bad"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "TOKEN_INVALID"
    chk = data(client.post(f"/api/control/drafts/{d['draft_id']}/check"))
    assert chk["check"]["verdict"] in ("pass", "warn")
    ap = data(client.post(f"/api/control/drafts/{d['draft_id']}/approve", json={"approver": "张调度"}))
    token = ap["approval"]["token"]
    cmd = data(client.post(f"/api/control/drafts/{d['draft_id']}/execute", json={"token": token}))
    s = data(client.get("/api/control/settings/ST02"))
    assert s["mode"] == "manual" and abs(s["valve_set"] - target) < 0.01
    # 令牌一次性
    r = client.post(f"/api/control/drafts/{d['draft_id']}/execute", json={"token": token})
    assert r.status_code == 400
    eff = data(client.get(f"/api/control/commands/{cmd['command_id']}/effect"))
    assert "tracking" in eff
    data(client.post(f"/api/control/commands/{cmd['command_id']}/rollback", json={"reason": "test"}))
    s = data(client.get("/api/control/settings/ST02"))
    assert s["mode"] == cur["mode"]
    audit = data(client.get("/api/audit"))
    assert {"draft", "safety_check", "approve", "execute", "rollback"} <= {a["action"] for a in audit}


def test_curve_draft_l3_autosign(client):
    cur = data(client.get("/api/control/settings/ST01"))
    curve = [[t, y - 1.0] for t, y in cur["curve"]]
    d = data(client.post("/api/control/drafts", json={"station_id": "ST01", "param": "climate_curve", "value": curve}))
    chk = data(client.post(f"/api/control/drafts/{d['draft_id']}/check"))
    assert chk["check"]["verdict"] == "pass"
    assert chk["status"] == "approved" and chk["approval"]["approver"].startswith("安全网关")


def test_curve_non_monotonic_blocked(client):
    cur = data(client.get("/api/control/settings/ST05"))
    curve = [list(p) for p in cur["curve"]]
    curve[-1][1] = curve[0][1] + 1
    d = data(client.post("/api/control/drafts", json={"station_id": "ST05", "param": "climate_curve", "value": curve}))
    chk = data(client.post(f"/api/control/drafts/{d['draft_id']}/check"))
    assert chk["check"]["verdict"] == "block"


# ---------------- 统计 ----------------
def test_analytics(client):
    d = data(client.get("/api/analytics/daily?scope=NET"))
    assert d["summary"]["heat_gj"] > 0 and d["findings"]
    e = data(client.get("/api/analytics/energy?days=7"))
    assert len(e["stations"]) == 13 and e["network"]["norm_heat"] > 0
    n = e["network"]
    # 单位换算：GJ × 34.12 kgce/GJ = kgce；/1000 = tce；kgce × 2.66 = kgCO2；/1000 = t
    assert abs(n["saved_tce"] - n["saved_gj"] * 34.12 / 1000) < 0.05
    assert abs(n["saved_tco2"] - n["saved_tce"] * 2.66) < 0.5
    m = data(client.get("/api/analytics/multidim?metric=heat_gj&level=source&grain=hour&days=3"))
    assert len(m["rows"]) == 3 and len(m["labels"]) == 24


# ---------------- AI ----------------
def test_forecast_intervals(client):
    f = data(client.get("/api/ai/forecast?scope=HS01&horizon=48"))
    p10, p50, p90 = (np.array(f["forecast"][k]) for k in ("p10", "p50", "p90"))
    assert len(p50) == 48 and np.all(p10 <= p50) and np.all(p50 <= p90)
    assert f["backtest"]["mape"] < 10


def test_anomalies_find_injected_faults(client):
    a = data(client.get("/api/ai/anomalies"))
    found = {e["station_id"]: e for e in a["events"] if not e["known_cause"]}
    assert "ST03" in found and found["ST03"]["feature"] == "heat_balance"
    assert "ST07" in found and found["ST07"]["feature"] == "makeup"


def test_curve_opt_direction(client):
    hot = data(client.get("/api/ai/curve/ST02"))
    cold = data(client.get("/api/ai/curve/ST10"))
    assert np.mean(hot["deltas"]) < -1 and hot["saving_pct"] > 0
    assert np.mean(cold["deltas"]) > 1
    assert hot["model"]["r2"] > 0.9


def test_effect_and_diagnosis(client):
    e = data(client.get("/api/ai/effect/ST01"))
    assert e["did_pct"] < 0
    d = data(client.get("/api/ai/diagnosis"))
    assert d["findings"] and len(d["scores"]) == 13
