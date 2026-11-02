"""熔断：同一管网 1 小时内回滚超过设定次数，或数据缺失率超阈值，自动将该管网降级为 L1（设计文档第十一节）。

管网 = 一个热源及其所带热力站（HS01 与 ST01–ST06 等），同一管网的站在水力上相互影响，所以按管网熔断。

状态：normal（正常）⇄ tripped（已熔断）
  触发（任一）：
    · 滑动窗口内回滚次数 ≥ 上限（所有回滚都计：回滚本身就说明调控结果不符合预期）
    · 窗口内数据缺失率 > 阈值，且窗口内样本已覆盖足够时长（防止启动时样本太少误触发）
  熔断后：有效自治等级降为 L1——不自动签发令牌；智能体不能提交审批（R2）或下发（R3），草拟与安全校验（R1）仍可用；
          已签发未执行的令牌作废；智能体发起的待审批请求取消；人工操作照常；现场设备保持当前设定，不自动回滚。
  解除：只能由有批准权的人工会话复核后解除；最近一段时间数据缺失率必须已回到阈值以下；
        解除时清零回滚计数基线（否则窗口内的旧回滚会让它立刻再次熔断）。不会自动解除。

阈值可用环境变量调整：HEATNET_BREAKER_ROLLBACKS（默认 3）、HEATNET_BREAKER_WINDOW_MIN（60）、
HEATNET_BREAKER_MISSING（0.2）、HEATNET_BREAKER_MIN_COVERAGE_MIN（10）、HEATNET_BREAKER_RESET_WINDOW_MIN（10）。
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from ..config import SOURCES, STATION_BY_ID
from ..sim.history import TZ


def _env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class BreakerConfig:
    rollback_limit: int = int(_env("HEATNET_BREAKER_ROLLBACKS", 3))
    window_s: float = _env("HEATNET_BREAKER_WINDOW_MIN", 60) * 60
    missing_limit: float = _env("HEATNET_BREAKER_MISSING", 0.2)
    min_coverage_s: float = _env("HEATNET_BREAKER_MIN_COVERAGE_MIN", 10) * 60
    reset_window_s: float = _env("HEATNET_BREAKER_RESET_WINDOW_MIN", 10) * 60


class BreakerError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass
class NetState:
    network_id: str
    state: str = "normal"                         # normal | tripped
    rollbacks: deque = field(default_factory=deque)      # (ts, station_id, command_id)
    samples: deque = field(default_factory=deque)        # (ts, dt, 缺失站数, 总站数)
    tripped_at: float | None = None
    reasons: list[str] = field(default_factory=list)
    manual: bool = False
    trip_count: int = 0
    events: list[dict] = field(default_factory=list)


class CircuitBreaker:
    def __init__(self, config: BreakerConfig | None = None, clock: Callable[[], float] = time.time):
        self.cfg = config or BreakerConfig()
        self.clock = clock
        self.lock = threading.RLock()
        self.nets: dict[str, NetState] = {s.id: NetState(s.id) for s in SOURCES}
        self.on_trip: list[Callable[[str, list[str]], None]] = []
        self.on_reset: list[Callable[[str, str], None]] = []

    # ---------- 映射 ----------
    @staticmethod
    def network_of(object_id: str) -> str:
        return STATION_BY_ID[object_id].source_id if object_id in STATION_BY_ID else object_id

    def is_tripped(self, object_id: str) -> bool:
        n = self.nets.get(self.network_of(object_id))
        return bool(n and n.state == "tripped")

    def effective_autonomy(self, station_id: str) -> str:
        return "L1" if self.is_tripped(station_id) else STATION_BY_ID[station_id].autonomy

    # ---------- 输入 ----------
    def record_rollback(self, station_id: str, command_id: str):
        with self.lock:
            n = self.nets[self.network_of(station_id)]
            n.rollbacks.append((self.clock(), station_id, command_id))
            self._evaluate(n)

    def record_sample(self, online: dict[str, bool], dt: float):
        """每个实时 tick 调用一次：online = {站点: 是否在线}，dt = 距上次采样秒数。"""
        now = self.clock()
        with self.lock:
            for n in self.nets.values():
                sts = [s for s in online if self.network_of(s) == n.network_id]
                if sts:
                    n.samples.append((now, dt, sum(1 for s in sts if not online[s]), len(sts)))
            for n in self.nets.values():
                self._evaluate(n)

    # ---------- 指标 ----------
    def _prune(self, n: NetState, now: float):
        while n.rollbacks and n.rollbacks[0][0] < now - self.cfg.window_s:
            n.rollbacks.popleft()
        while n.samples and n.samples[0][0] < now - self.cfg.window_s:
            n.samples.popleft()

    def _rollback_count(self, n: NetState, now: float) -> int:
        return sum(1 for ts, *_ in n.rollbacks if ts >= now - self.cfg.window_s)

    @staticmethod
    def _missing(samples, since: float) -> tuple[float | None, float]:
        """时间加权缺失率与样本覆盖时长。"""
        sel = [(dt, miss, tot) for ts, dt, miss, tot in samples if ts >= since]
        cover = sum(dt for dt, _, _ in sel)
        if cover <= 0:
            return None, 0.0
        return sum(dt * miss / tot for dt, miss, tot in sel) / cover, cover

    def metrics(self, network_id: str) -> dict:
        now = self.clock()
        with self.lock:
            n = self.nets[network_id]
            self._prune(n, now)
            miss, cover = self._missing(n.samples, now - self.cfg.window_s)
            recent, recent_cover = self._missing(n.samples, now - self.cfg.reset_window_s)
            return {"rollbacks": self._rollback_count(n, now), "missing_rate": miss, "coverage_s": round(cover),
                    "recent_missing_rate": recent, "recent_coverage_s": round(recent_cover)}

    # ---------- 判定 ----------
    def _evaluate(self, n: NetState):
        if n.state == "tripped":
            return
        now = self.clock()
        self._prune(n, now)
        reasons = []
        rb = self._rollback_count(n, now)
        if rb >= self.cfg.rollback_limit:
            reasons.append(f"{int(self.cfg.window_s // 60)} 分钟内回滚 {rb} 次（上限 {self.cfg.rollback_limit} 次）")
        miss, cover = self._missing(n.samples, now - self.cfg.window_s)
        if miss is not None and cover >= self.cfg.min_coverage_s and miss > self.cfg.missing_limit:
            reasons.append(f"近 {int(cover // 60)} 分钟数据缺失率 {miss:.0%}（阈值 {self.cfg.missing_limit:.0%}）")
        if reasons:
            self._trip(n, reasons, manual=False, by="安全网关（熔断）")

    def _trip(self, n: NetState, reasons: list[str], manual: bool, by: str):
        n.state, n.tripped_at, n.reasons, n.manual = "tripped", self.clock(), reasons, manual
        n.trip_count += 1
        n.events.insert(0, {"ts": _iso(n.tripped_at), "action": "trip", "by": by, "reasons": reasons, "manual": manual})
        for cb in self.on_trip:
            cb(n.network_id, reasons)

    def trip(self, network_id: str, by: str, reason: str) -> dict:
        """人工熔断（如检修、发现数据异常时主动收回自动化权限）。"""
        with self.lock:
            n = self._net(network_id)
            if n.state == "tripped":
                raise BreakerError("ALREADY_TRIPPED", f"{network_id} 已处于熔断状态")
            self._trip(n, [f"人工熔断：{reason or '未填写原因'}"], manual=True, by=by)
            return self.state(network_id)

    def reset(self, network_id: str, by: str, comment: str) -> dict:
        with self.lock:
            n = self._net(network_id)
            if n.state != "tripped":
                raise BreakerError("NOT_TRIPPED", f"{network_id} 未处于熔断状态")
            if not (comment or "").strip():
                raise BreakerError("COMMENT_REQUIRED", "解除熔断须填写复核结论")
            now = self.clock()
            recent, cover = self._missing(n.samples, now - self.cfg.reset_window_s)
            if recent is not None and recent > self.cfg.missing_limit:
                raise BreakerError("CONDITION_PERSISTS",
                                   f"最近 {int(cover // 60) or 1} 分钟数据缺失率仍为 {recent:.0%}，高于阈值 "
                                   f"{self.cfg.missing_limit:.0%}，请先恢复数据采集再解除")
            n.state, n.reasons, n.manual, n.tripped_at = "normal", [], False, None
            n.rollbacks.clear()                     # 复核前的回滚不再计入，避免立刻再次熔断
            n.events.insert(0, {"ts": _iso(now), "action": "reset", "by": by, "comment": comment})
            for cb in self.on_reset:
                cb(network_id, by)
            return self.state(network_id)

    def _net(self, network_id: str) -> NetState:
        n = self.nets.get(network_id)
        if not n:
            raise BreakerError("NOT_FOUND", f"管网 {network_id} 不存在")
        return n

    # ---------- 状态 ----------
    def state(self, network_id: str) -> dict:
        n = self.nets[network_id]
        m = self.metrics(network_id)
        return {"network_id": network_id, "state": n.state, "tripped_at": _iso(n.tripped_at) if n.tripped_at else None,
                "reasons": n.reasons, "manual": n.manual, "trip_count": n.trip_count, "events": n.events[:20],
                "metrics": m, "limits": {"rollbacks": self.cfg.rollback_limit, "window_min": self.cfg.window_s / 60,
                                         "missing_rate": self.cfg.missing_limit,
                                         "min_coverage_min": self.cfg.min_coverage_s / 60,
                                         "reset_window_min": self.cfg.reset_window_s / 60}}

    def summary(self) -> dict[str, dict]:
        return {k: {"state": n.state, "reasons": n.reasons} for k, n in self.nets.items()}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, TZ).replace(tzinfo=None).isoformat(timespec="seconds")
