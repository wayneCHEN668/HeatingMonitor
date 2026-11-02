"""调控执行链路：草拟 → 安全校验 → 审批令牌 → 下发 → 效果跟踪 / 回滚。

对应设计文档第六节 control_* 工具。关键约束：
- 没有有效令牌，execute 一律拒绝；令牌与 draft_id 绑定、有有效期、一次性；
- 安全校验在服务端完成（限值/变化率/频次/联锁/仿真验证），不依赖调用方“自觉”；
- L3/L4 站点在校验 pass 时由安全网关自动签发令牌；
- 每一步写审计日志。
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import secrets
import time
import uuid
from datetime import timedelta

import numpy as np

from ..config import CURVE_TOUT, LIMITS, STATION_BY_ID
from ..sim.history import now_local
from ..sim.realtime import Engine

SECRET = secrets.token_bytes(32)
TOKEN_TTL = 600  # s

PARAM_TEXT = {
    "valve_opening": "调节阀开度", "control_mode": "控制模式", "supply_temp_setpoint": "二次供温设定",
    "climate_curve": "气候补偿曲线", "pump_frequency": "二次循环泵频率",
}
MODE_TEXT = {"curve": "气候补偿", "fixed": "定供温", "manual": "手动阀位"}


class ControlError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class ControlService:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.drafts: dict[str, dict] = {}
        self.commands: dict[str, dict] = {}
        self.audit: list[dict] = []
        self.used_tokens: set[str] = set()

    # ---------------- 审计 ----------------
    def record_tool_call(self, agent: str, user: str, tool: str, args: dict, ok: bool, summary: str,
                         trace_id: str = "", risk: str = "") -> None:
        """记录智能体工具调用（谁问的、哪个智能体、调了什么、入参、结论）。"""
        self._audit("tool_call", f"{agent}（代 {user}）", str(args.get("station_id") or args.get("object_id")
                    or args.get("scope_id") or args.get("draft_id") or args.get("command_id") or "-"),
                    {"tool": tool, "risk": risk, "args": args, "summary": summary[:200], "trace_id": trace_id},
                    "ok" if ok else "error")

    def _audit(self, action: str, user: str, target: str, detail: dict, result: str):
        self.audit.insert(0, {"id": uuid.uuid4().hex[:10], "ts": now_local().isoformat(timespec="seconds"),
                              "action": action, "user": user, "target": target, "detail": detail,
                              "result": result})
        del self.audit[2000:]

    # ---------------- 策略 ----------------
    def policy(self, station_id: str) -> dict:
        cfg = self._cfg(station_id)
        lim = LIMITS
        return {
            "station_id": station_id, "autonomy": cfg.autonomy,
            "auto_sign": cfg.autonomy in ("L3", "L4"),
            "params": {
                "valve_opening": {"min": lim["valve"][0], "max": lim["valve"][1], "max_step": lim["valve_step"], "unit": "%"},
                "supply_temp_setpoint": {"min": lim[cfg.system]["ts2"][0], "max": lim[cfg.system]["ts2"][1],
                                         "max_step": lim["ts2_step"], "unit": "℃"},
                "climate_curve": {"min": lim[cfg.system]["ts2"][0], "max": lim[cfg.system]["ts2"][1],
                                  "max_point_step": lim["curve_point_step"], "tout_points": CURVE_TOUT, "unit": "℃"},
                "pump_frequency": {"min": lim["freq"][0], "max": lim["freq"][1], "max_step": lim["freq_step"], "unit": "Hz"},
                "control_mode": {"options": list(MODE_TEXT)},
            },
            "max_cmds_per_hour": lim["max_cmds_per_hour"],
            "interlocks": [f"室外温度低于 {lim['freeze_tout']}℃ 时阀门开度不得低于 {lim['freeze_valve']}%（防冻）",
                           "气候补偿曲线须随室外温度升高单调不增",
                           f"仿真预测室温低于 {lim['tin_block']}℃ 时禁止下发"],
            "approvers": ["值班调度长"],
        }

    def _cfg(self, station_id):
        cfg = STATION_BY_ID.get(station_id)
        if not cfg:
            raise ControlError("NOT_FOUND", f"站点 {station_id} 不存在")
        return cfg

    def current_settings(self, station_id: str) -> dict:
        s = self.engine.st[station_id]
        return {"mode": s.mode, "curve": copy.deepcopy(s.curve), "ts2_fixed": s.ts2_fixed,
                "valve_set": round(s.valve_set, 1), "freq": s.freq}

    # ---------------- 1. 草拟 ----------------
    def draft(self, station_id: str, param: str, value, user: str, reason: str = "", source: str = "manual") -> dict:
        self._cfg(station_id)
        if param not in PARAM_TEXT:
            raise ControlError("BAD_PARAM", f"不支持的参数 {param}")
        cur = self.current_settings(station_id)
        override = {}
        if param == "valve_opening":
            value = float(value)
            override = {"mode": "manual", "valve_set": value}
            desc = f"切换为手动，阀门开度 {cur['valve_set']:.1f}% → {value:.1f}%"
        elif param == "control_mode":
            if value not in MODE_TEXT:
                raise ControlError("BAD_VALUE", "控制模式须为 curve / fixed / manual")
            override = {"mode": value}
            if value == "fixed":
                override["ts2_fixed"] = round(float(self.engine.snapshot["stations"][station_id]["ts2"]), 1)
            if value == "manual":
                override["valve_set"] = cur["valve_set"]
            desc = f"控制模式 {MODE_TEXT[cur['mode']]} → {MODE_TEXT[value]}"
        elif param == "supply_temp_setpoint":
            value = float(value)
            override = {"mode": "fixed", "ts2_fixed": value}
            desc = f"切换为定供温，二次供温设定 → {value:.1f}℃"
        elif param == "climate_curve":
            pts = [[float(p[0]), float(p[1])] for p in value]
            if [p[0] for p in pts] != [float(x) for x in CURVE_TOUT]:
                raise ControlError("BAD_VALUE", f"曲线横坐标须为 {CURVE_TOUT}")
            value = pts
            override = {"mode": "curve", "curve": pts}
            diffs = [round(n[1] - o[1], 1) for n, o in zip(pts, cur["curve"])]
            desc = f"更新气候补偿曲线（各点变化 {diffs} ℃）"
        else:  # pump_frequency
            value = float(value)
            override = {"freq": value}
            desc = f"二次循环泵频率 {cur['freq']:.1f} → {value:.1f} Hz"
        before = self.engine.predict(station_id, {})
        after = self.engine.predict(station_id, override)
        d = {
            "draft_id": "DR" + uuid.uuid4().hex[:8].upper(), "station_id": station_id,
            "station_name": STATION_BY_ID[station_id].name, "param": param, "param_text": PARAM_TEXT[param],
            "value": value, "override": override, "before": cur, "description": desc, "reason": reason,
            "source": source, "created_by": user, "created_at": now_local().isoformat(timespec="seconds"),
            "status": "drafted", "expected": {"before": before, "after": after},
            "check": None, "approval": None,
        }
        self.drafts[d["draft_id"]] = d
        self._audit("draft", user, station_id, {"draft_id": d["draft_id"], "desc": desc}, "ok")
        return d

    # ---------------- 2. 安全校验 ----------------
    def safety_check(self, draft_id: str, user: str = "system") -> dict:
        d = self._draft(draft_id)
        if d["status"] in ("executed", "rejected", "expired"):
            raise ControlError("BAD_STATE", f"草案状态为 {d['status']}，不能再次校验")
        cfg = STATION_BY_ID[d["station_id"]]
        lim = LIMITS
        cur = d["before"]
        tout = self.engine.snapshot["tout"]
        items = []

        def add(name, status, msg):
            items.append({"name": name, "status": status, "message": msg})

        p, v = d["param"], d["value"]
        lo, hi = lim[cfg.system]["ts2"]
        if p == "valve_opening":
            ok = lim["valve"][0] <= v <= lim["valve"][1]
            add("限值校验", "pass" if ok else "block", f"开度 {v:.1f}% {'在' if ok else '超出'}允许范围 {lim['valve']}")
            step = abs(v - cur["valve_set"])
            add("变化率校验", "pass" if step <= lim["valve_step"] else "block",
                f"单次调整 {step:.1f}%（上限 {lim['valve_step']}%）")
            frz = tout < lim["freeze_tout"] and v < lim["freeze_valve"]
            add("防冻联锁", "block" if frz else "pass",
                f"室外 {tout:.1f}℃，{'开度过小有冻管风险' if frz else '满足防冻要求'}")
        elif p == "supply_temp_setpoint":
            ok = lo <= v <= hi
            add("限值校验", "pass" if ok else "block", f"设定 {v:.1f}℃，允许范围 [{lo}, {hi}]")
            ref = self.engine.snapshot["stations"][d["station_id"]]["ts2_set"]
            step = abs(v - ref)
            add("变化率校验", "pass" if step <= lim["ts2_step"] else "block",
                f"相对当前设定 {ref:.1f}℃ 调整 {step:.1f}℃（上限 {lim['ts2_step']}℃）")
        elif p == "climate_curve":
            ys = [pt[1] for pt in v]
            ok = all(lo <= y <= hi for y in ys)
            add("限值校验", "pass" if ok else "block", f"曲线取值 {min(ys):.1f}–{max(ys):.1f}℃，允许 [{lo}, {hi}]")
            mono = all(ys[i] >= ys[i + 1] - 1e-6 for i in range(len(ys) - 1))
            add("曲线形态联锁", "pass" if mono else "block", "随室外温度升高单调不增" if mono else "曲线出现反向（室外升温供温反而升高）")
            step = max(abs(n[1] - o[1]) for n, o in zip(v, cur["curve"]))
            st = "block" if step > lim["curve_point_step"] else ("warn" if step > lim["curve_point_warn"] else "pass")
            add("变化率校验", st, f"单点最大调整 {step:.1f}℃（告警 {lim['curve_point_warn']}℃ / 上限 {lim['curve_point_step']}℃）")
        elif p == "pump_frequency":
            ok = lim["freq"][0] <= v <= lim["freq"][1]
            add("限值校验", "pass" if ok else "block", f"频率 {v:.1f}Hz，允许 {lim['freq']}")
            step = abs(v - cur["freq"])
            add("变化率校验", "pass" if step <= lim["freq_step"] else "block", f"单次调整 {step:.1f}Hz（上限 {lim['freq_step']}Hz）")
        else:
            add("限值校验", "pass", f"模式切换为 {MODE_TEXT[v]}")

        # 频次
        hour_ago = time.time() - 3600
        n = sum(1 for c in self.commands.values()
                if c["station_id"] == d["station_id"] and c["executed_epoch"] > hour_ago)
        add("频次校验", "pass" if n < lim["max_cmds_per_hour"] else "block",
            f"近 1 小时已下发 {n} 次（上限 {lim['max_cmds_per_hour']}）")
        # 通讯
        online = self.engine.snapshot["stations"][d["station_id"]]["online"]
        add("通讯状态", "pass" if online else "block", "站点在线" if online else "站点通讯中断，无法确认回执")
        # 仿真验证
        exp = d["expected"]["after"]
        tin = exp["tin"]
        if tin < lim["tin_block"]:
            st = "block"
        elif tin < lim["tin_warn"][0] or tin > lim["tin_warn"][1]:
            st = "warn"
        else:
            st = "pass"
        msg = f"稳态预测室温 {tin:.1f}℃、二次供温 {exp['ts2']:.1f}℃、热功率 {exp['heat_mw']:.2f}MW"
        if exp.get("saturated"):
            st = "warn" if st == "pass" else st
            msg += "；阀门将饱和，设定可能达不到"
        add("仿真验证", st, msg)

        order = {"pass": 0, "warn": 1, "block": 2}
        verdict = max((i["status"] for i in items), key=lambda s: order[s])
        d["check"] = {"verdict": verdict, "items": items, "ts": now_local().isoformat(timespec="seconds")}
        d["status"] = "checked" if verdict != "block" else "blocked"
        self._audit("safety_check", user, d["station_id"], {"draft_id": draft_id, "verdict": verdict}, verdict)
        # L3/L4：安全边界内自动签发
        if verdict == "pass" and cfg.autonomy in ("L3", "L4"):
            self._sign(d, "安全网关(自动)", "L3 站点安全边界内自动签发")
        return d

    # ---------------- 3. 审批签发令牌 ----------------
    def _sign(self, d: dict, approver: str, comment: str):
        exp = int(time.time()) + TOKEN_TTL
        msg = f"{d['draft_id']}|{exp}".encode()
        sig = hmac.new(SECRET, msg, hashlib.sha256).hexdigest()[:24]
        token = f"{d['draft_id']}.{exp}.{sig}"
        d["approval"] = {"approver": approver, "comment": comment, "token": token,
                         "expires_at": (now_local() + timedelta(seconds=TOKEN_TTL)).isoformat(timespec="seconds"),
                         "ts": now_local().isoformat(timespec="seconds")}
        d["status"] = "approved"
        self._audit("approve", approver, d["station_id"], {"draft_id": d["draft_id"], "comment": comment}, "signed")

    def approve(self, draft_id: str, approver: str, comment: str = "") -> dict:
        """人工签发令牌。只供人机界面调用，不暴露给智能体（MCP 工具层没有对应工具）。"""
        d = self._draft(draft_id)
        self._expire(d)
        if not d["check"]:
            raise ControlError("NOT_CHECKED", "草案尚未通过安全校验")
        if d["check"]["verdict"] == "block":
            raise ControlError("BLOCKED", "安全校验未通过（block），不能签发令牌")
        if d["status"] in ("executed", "rejected", "expired"):
            raise ControlError("BAD_STATE", f"草案状态为 {d['status']}")
        self._sign(d, approver, comment or "人工确认")
        if d.get("approval_request"):
            d["approval_request"]["resolved"] = "approved"
        return d

    def request_approval(self, draft_id: str, user: str, reason: str = "", timeout_s: int = 900) -> dict:
        """提交审批（R2）：只登记待审批并通知值班调度，不签发令牌。

        智能体调用这个接口后，必须等待人工在界面上批准，才能拿到一次性令牌。
        L3/L4 站点在安全校验 pass 时已由安全网关自动签发，这里直接返回现状。
        """
        d = self._draft(draft_id)
        self._expire(d)
        if d["status"] == "approved":
            return d
        if not d["check"]:
            raise ControlError("NOT_CHECKED", "草案尚未做安全校验，请先调用安全校验")
        if d["check"]["verdict"] == "block":
            raise ControlError("BLOCKED", "安全校验结论为 block，不能提交审批")
        if d["status"] != "checked" and d["status"] != "pending_approval":
            raise ControlError("BAD_STATE", f"草案状态为 {d['status']}，不能提交审批")
        if d["status"] == "pending_approval":
            return d
        timeout_s = int(min(max(timeout_s, 60), 3600))
        now = now_local()
        d["approval_request"] = {
            "approval_id": "AP" + uuid.uuid4().hex[:8].upper(), "requested_by": user, "reason": reason,
            "ts": now.isoformat(timespec="seconds"),
            "expires_at": (now + timedelta(seconds=timeout_s)).isoformat(timespec="seconds"),
            "approvers": self.policy(d["station_id"])["approvers"], "resolved": None,
        }
        d["status"] = "pending_approval"
        self._audit("request_approval", user, d["station_id"],
                    {"draft_id": draft_id, "approval_id": d["approval_request"]["approval_id"], "reason": reason},
                    "pending")
        return d

    def _expire(self, d: dict):
        req = d.get("approval_request")
        if d["status"] == "pending_approval" and req and req["expires_at"] < now_local().isoformat(timespec="seconds"):
            d["status"] = "expired"
            req["resolved"] = "expired"
            self._audit("approval_expired", "system", d["station_id"], {"draft_id": d["draft_id"]}, "expired")

    def get_draft(self, draft_id: str) -> dict:
        d = self._draft(draft_id)
        self._expire(d)
        return d

    def pending_approvals(self) -> list[dict]:
        out = []
        for d in self.list_drafts():
            self._expire(d)
            if d["status"] == "pending_approval":
                out.append(d)
        return out

    def reject(self, draft_id: str, user: str, comment: str = "") -> dict:
        d = self._draft(draft_id)
        if d["status"] == "executed":
            raise ControlError("BAD_STATE", "草案已执行，不能驳回（如需撤销请回滚指令）")
        d["status"] = "rejected"
        if d.get("approval_request"):
            d["approval_request"]["resolved"] = "rejected"
        self._audit("reject", user, d["station_id"], {"draft_id": draft_id, "comment": comment}, "rejected")
        return d

    def _verify(self, token: str, draft_id: str) -> tuple[bool, str]:
        try:
            did, exp, sig = token.split(".")
        except ValueError:
            return False, "令牌格式错误"
        if did != draft_id:
            return False, "令牌与草案不匹配"
        if int(exp) < time.time():
            return False, "令牌已过期"
        good = hmac.new(SECRET, f"{did}|{exp}".encode(), hashlib.sha256).hexdigest()[:24]
        if not hmac.compare_digest(good, sig):
            return False, "令牌签名无效"
        if token in self.used_tokens:
            return False, "令牌已使用"
        return True, ""

    # ---------------- 4. 下发 ----------------
    def execute(self, draft_id: str, token: str, user: str) -> dict:
        d = self._draft(draft_id)
        ok, why = self._verify(token or "", draft_id)
        if not ok:
            self._audit("execute", user, d["station_id"], {"draft_id": draft_id}, f"rejected: {why}")
            raise ControlError("TOKEN_INVALID", f"令牌无效：{why}，未下发")
        self.used_tokens.add(token)
        before = self.current_settings(d["station_id"])
        self._apply(d["station_id"], d["override"])
        cmd = {
            "command_id": "CMD" + uuid.uuid4().hex[:8].upper(), "draft_id": draft_id,
            "station_id": d["station_id"], "station_name": d["station_name"], "param": d["param"],
            "param_text": d["param_text"], "value": d["value"], "description": d["description"],
            "before": before, "executed_by": user, "approver": d["approval"]["approver"],
            "executed_at": now_local().isoformat(timespec="seconds"), "executed_epoch": time.time(),
            "ack": {"status": "acked", "latency_ms": int(80 + np.random.rand() * 220)},
            "status": "executed", "expected": d["expected"]["after"], "source": d["source"],
            "start_snapshot": {k: self.engine.snapshot["stations"][d["station_id"]][k]
                               for k in ("ts2", "tr2", "valve", "tin", "heat_mw", "ts2_set")},
        }
        self.commands[cmd["command_id"]] = cmd
        d["status"] = "executed"
        d["command_id"] = cmd["command_id"]
        self._audit("execute", user, d["station_id"], {"command_id": cmd["command_id"], "desc": d["description"]}, "acked")
        return cmd

    def _apply(self, station_id: str, override: dict):
        s = self.engine.st[station_id]
        if "curve" in override:
            s.curve = [list(p) for p in override["curve"]]
        if "ts2_fixed" in override:
            s.ts2_fixed = float(override["ts2_fixed"])
        if "valve_set" in override:
            s.valve_set = float(override["valve_set"])
        if "freq" in override:
            s.freq = float(override["freq"])
        if "mode" in override:
            s.mode = override["mode"]

    # ---------------- 5. 效果跟踪 / 回滚 ----------------
    def monitor(self, command_id: str) -> dict:
        c = self._cmd(command_id)
        r = self.engine.snapshot["stations"][c["station_id"]]
        elapsed = time.time() - c["executed_epoch"]
        exp = c["expected"]
        track = {
            "elapsed_s": int(elapsed),
            "valve": {"now": r["valve"], "target": exp["valve"]},
            "ts2": {"now": r["ts2"], "target": exp["ts2_set"] if exp["ts2_set"] is not None else exp["ts2"]},
            "tin": {"now": r["tin"], "expected": exp["tin"], "start": c["start_snapshot"]["tin"]},
            "heat_mw": {"now": r["heat_mw"], "expected": exp["heat_mw"], "start": c["start_snapshot"]["heat_mw"]},
        }
        dev = abs(r["ts2"] - track["ts2"]["target"])
        settled = abs(r["valve"] - exp["valve"]) < 1.0 and dev < 1.0
        suggest_rollback = elapsed > 600 and dev > 5
        return {"command": c, "tracking": track, "settled": settled, "suggest_rollback": suggest_rollback}

    def rollback(self, command_id: str, user: str, reason: str = "") -> dict:
        c = self._cmd(command_id)
        if c["status"] == "rolled_back":
            raise ControlError("BAD_STATE", "该指令已回滚")
        b = c["before"]
        self._apply(c["station_id"], {"curve": b["curve"], "ts2_fixed": b["ts2_fixed"], "valve_set": b["valve_set"],
                                      "freq": b["freq"], "mode": b["mode"]})
        c["status"] = "rolled_back"
        c["rollback"] = {"by": user, "reason": reason, "ts": now_local().isoformat(timespec="seconds")}
        self._audit("rollback", user, c["station_id"], {"command_id": command_id, "reason": reason}, "ok")
        return c

    # ---------------- 查询 ----------------
    def _draft(self, draft_id):
        d = self.drafts.get(draft_id)
        if not d:
            raise ControlError("NOT_FOUND", f"草案 {draft_id} 不存在")
        return d

    def _cmd(self, command_id):
        c = self.commands.get(command_id)
        if not c:
            raise ControlError("NOT_FOUND", f"指令 {command_id} 不存在")
        return c

    def list_commands(self, station_id: str | None = None):
        cs = sorted(self.commands.values(), key=lambda c: c["executed_epoch"], reverse=True)
        return [c for c in cs if station_id in (None, c["station_id"])]

    def list_drafts(self, station_id: str | None = None):
        ds = sorted(self.drafts.values(), key=lambda d: d["created_at"], reverse=True)
        return [d for d in ds if station_id in (None, d["station_id"])]
