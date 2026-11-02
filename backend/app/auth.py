"""身份认证、角色权限（RBAC）、数据范围与智能体委托授权。

对应设计文档第十一节“安全护栏”：
- 权限不靠提示词约束，而在服务端强制执行：每个接口声明所需权限；
- 智能体权限 = 智能体白名单 ∩ 当前用户权限：用户为智能体签发“委托令牌”，令牌里带智能体档案（dispatch/analyst）；
- 数据范围：按热源片区限定可见对象，实时、历史、统计、AI、调控一律过滤；
- “批准”只接受人工会话：任何智能体档案的白名单里都没有 control:approve，委托令牌永远拿不到这个权限。

演示环境：用户存在内存里，密码用 PBKDF2 存储；令牌用 HMAC-SHA256 签名。
签名密钥取环境变量 HEATNET_SECRET，未设置时每次启动随机生成（重启后所有令牌失效）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from .config import SOURCES, STATIONS
from .sim.history import now_local

# ---------------------------------------------------------------- 权限与角色
PERMISSIONS: dict[str, str] = {
    "monitor:read": "查看实时监控、工艺图、报警",
    "analytics:read": "查看统计分析",
    "ai:read": "查看 AI 分析与预测",
    "alarm:ack": "确认报警",
    "control:draft": "草拟调控指令、安全校验、提交审批",
    "control:execute": "凭令牌下发指令、回滚",
    "control:approve": "批准或驳回调控请求（签发令牌）",
    "audit:read": "查看审计日志",
    "agent:delegate": "为智能体签发委托令牌",
}
_ALL = frozenset(PERMISSIONS)
_VIEW = {"monitor:read", "analytics:read", "ai:read"}

ROLES: dict[str, tuple[str, frozenset[str]]] = {
    "admin": ("系统管理员", _ALL),
    "chief": ("值班调度长", frozenset(_VIEW | {"alarm:ack", "control:draft", "control:execute", "control:approve",
                                              "audit:read", "agent:delegate"})),
    "dispatcher": ("调度员", frozenset(_VIEW | {"alarm:ack", "control:draft", "control:execute", "agent:delegate"})),
    "manager": ("经营管理", frozenset(_VIEW | {"agent:delegate"})),
    "stationmaster": ("片区站长", frozenset(_VIEW | {"alarm:ack", "agent:delegate"})),
    "service": ("客服坐席", frozenset({"monitor:read", "agent:delegate"})),
}

# 智能体档案白名单（设计文档第九节：只有调度智能体可以使用 control_*；任何智能体都不能批准）
AGENT_PROFILES: dict[str, tuple[str, frozenset[str]]] = {
    "dispatch": ("调度智能体", frozenset(_VIEW | {"control:draft", "control:execute"})),
    "analyst": ("分析智能体", frozenset(_VIEW)),
}
assert all("control:approve" not in p for _, p in AGENT_PROFILES.values())

SESSION_TTL = 12 * 3600
AGENT_TTL_MAX_DAYS = 90


@dataclass
class User:
    username: str
    name: str
    role: str
    sources: frozenset[str] | None          # None = 全网；否则为可见热源片区
    pw_hash: str
    salt: str


# 演示账号（生产环境替换为企业统一身份认证 / LDAP / OAuth）
DEMO_USERS = [
    ("admin", "系统管理员", "admin", None, "admin123"),
    ("chief", "李调度长", "chief", None, "chief123"),
    ("dispatcher", "张调度", "dispatcher", None, "disp123"),
    ("manager", "王经理", "manager", None, "mgr123"),
    ("zhao", "赵站长", "stationmaster", ["HS02"], "zhao123"),
    ("service", "刘客服", "service", None, "svc123"),
]


def _hash_pw(pw: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 60_000).hex()


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ---------------------------------------------------------------- 主体
@dataclass(frozen=True)
class Principal:
    username: str
    name: str
    role: str
    kind: str                                # human | agent
    perms: frozenset[str]
    sources: frozenset[str] | None           # None = 全网
    jti: str
    agent: str | None = None
    profile: str | None = None

    @property
    def role_name(self) -> str:
        return ROLES[self.role][0]

    @property
    def label(self) -> str:
        """写入审计与业务记录的身份。"""
        if self.kind == "agent":
            return f"{self.agent}（代 {self.name}）"
        return f"{self.name}（{self.role_name}）"

    @property
    def is_global(self) -> bool:
        return self.sources is None

    @property
    def visible_sources(self) -> list[str]:
        return [s.id for s in SOURCES if self.sources is None or s.id in self.sources]

    @property
    def visible_stations(self) -> list[str]:
        return [s.id for s in STATIONS if self.sources is None or s.source_id in self.sources]

    def can_see(self, oid: str) -> bool:
        return oid == "NET" or oid in self.visible_sources or oid in self.visible_stations

    def has(self, perm: str) -> bool:
        return perm in self.perms

    def public(self) -> dict:
        return {"username": self.username, "name": self.name, "role": self.role, "role_name": self.role_name,
                "kind": self.kind, "agent": self.agent, "profile": self.profile, "label": self.label,
                "permissions": sorted(self.perms),
                "scope": {"global": self.is_global, "sources": self.visible_sources,
                          "stations": self.visible_stations}}


class AuthError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# ---------------------------------------------------------------- 服务
@dataclass
class AuthService:
    secret: bytes = field(default_factory=lambda: os.environ.get("HEATNET_SECRET", "").encode() or secrets.token_bytes(32))
    users: dict[str, User] = field(default_factory=dict)
    agent_tokens: dict[str, dict] = field(default_factory=dict)   # jti -> 记录（不保存令牌本身）
    revoked: set[str] = field(default_factory=set)
    login_failures: dict[str, list[float]] = field(default_factory=dict)

    def __post_init__(self):
        for username, name, role, sources, pw in DEMO_USERS:
            salt = secrets.token_hex(8)
            self.users[username] = User(username, name, role, frozenset(sources) if sources else None,
                                        _hash_pw(pw, salt), salt)

    # ---------- 令牌 ----------
    def _sign(self, payload: dict) -> str:
        body = _b64(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode())
        sig = _b64(hmac.new(self.secret, body.encode(), hashlib.sha256).digest())
        return f"{body}.{sig}"

    def _decode(self, token: str) -> dict:
        try:
            body, sig = token.split(".")
            good = _b64(hmac.new(self.secret, body.encode(), hashlib.sha256).digest())
            if not hmac.compare_digest(good, sig):
                raise ValueError
            payload = json.loads(_unb64(body))
        except (ValueError, json.JSONDecodeError):
            raise AuthError(401, "INVALID_TOKEN", "登录凭证无效") from None
        if payload.get("exp", 0) < time.time():
            raise AuthError(401, "TOKEN_EXPIRED", "登录已过期，请重新登录")
        if payload.get("jti") in self.revoked:
            raise AuthError(401, "TOKEN_REVOKED", "凭证已注销或被撤销")
        return payload

    # ---------- 登录 ----------
    def login(self, username: str, password: str) -> tuple[str, Principal]:
        now = time.time()
        fails = [t for t in self.login_failures.get(username, []) if now - t < 300]
        if len(fails) >= 5:
            raise AuthError(429, "TOO_MANY_ATTEMPTS", "登录失败次数过多，请 5 分钟后再试")
        u = self.users.get(username)
        if not u or not hmac.compare_digest(u.pw_hash, _hash_pw(password, u.salt)):
            self.login_failures[username] = fails + [now]
            raise AuthError(401, "BAD_CREDENTIALS", "用户名或密码错误")
        self.login_failures.pop(username, None)
        payload = {"sub": u.username, "kind": "human", "jti": uuid.uuid4().hex, "exp": int(now + SESSION_TTL)}
        return self._sign(payload), self._principal(payload)

    def logout(self, p: Principal):
        self.revoked.add(p.jti)

    # ---------- 智能体委托 ----------
    def issue_agent_token(self, owner: Principal, agent: str, profile: str, days: int, note: str = "") -> tuple[str, dict]:
        if owner.kind != "human":
            raise AuthError(403, "FORBIDDEN", "智能体不能再签发委托令牌")
        if not owner.has("agent:delegate"):
            raise AuthError(403, "FORBIDDEN", "没有为智能体授权的权限")
        if profile not in AGENT_PROFILES:
            raise AuthError(400, "BAD_PROFILE", f"智能体档案须为 {list(AGENT_PROFILES)}")
        days = int(min(max(days, 1), AGENT_TTL_MAX_DAYS))
        agent = (agent or AGENT_PROFILES[profile][0]).strip()[:30]
        jti = uuid.uuid4().hex
        exp = int(time.time() + days * 86400)
        token = self._sign({"sub": owner.username, "kind": "agent", "agent": agent, "profile": profile,
                            "jti": jti, "exp": exp})
        eff = self._effective(self.users[owner.username], profile)
        rec = {"jti": jti, "owner": owner.username, "owner_name": owner.name, "agent": agent, "profile": profile,
               "profile_name": AGENT_PROFILES[profile][0], "note": note[:100],
               "permissions": sorted(eff), "created_at": now_local().isoformat(timespec="seconds"),
               "expires_at": _local_iso(exp), "revoked": False, "last_used": None}
        self.agent_tokens[jti] = rec
        return token, rec

    def list_agent_tokens(self, p: Principal) -> list[dict]:
        rows = [r for r in self.agent_tokens.values() if p.role == "admin" or r["owner"] == p.username]
        return sorted(rows, key=lambda r: r["created_at"], reverse=True)

    def revoke_agent_token(self, p: Principal, jti: str) -> dict:
        r = self.agent_tokens.get(jti)
        if not r or (p.role != "admin" and r["owner"] != p.username):
            raise AuthError(404, "NOT_FOUND", "委托令牌不存在")
        if p.kind != "human":
            raise AuthError(403, "FORBIDDEN", "智能体不能撤销委托令牌")
        r["revoked"] = True
        r["revoked_at"] = now_local().isoformat(timespec="seconds")
        self.revoked.add(jti)
        return r

    # ---------- 解析主体 ----------
    @staticmethod
    def _effective(u: User, profile: str | None) -> frozenset[str]:
        role_perms = ROLES[u.role][1]
        if profile is None:
            return role_perms
        return role_perms & AGENT_PROFILES[profile][1]     # 智能体白名单 ∩ 用户权限

    def _principal(self, payload: dict) -> Principal:
        u = self.users.get(payload.get("sub", ""))
        if not u:
            raise AuthError(401, "INVALID_TOKEN", "用户不存在")
        kind = payload.get("kind")
        if kind == "agent":
            rec = self.agent_tokens.get(payload["jti"])
            if rec is None:
                # 签名正确但不在登记簿中（例如服务重启后内存登记丢失）：一律拒绝
                raise AuthError(401, "TOKEN_REVOKED", "委托令牌未登记或已失效，请重新签发")
            rec["last_used"] = now_local().isoformat(timespec="seconds")
            return Principal(u.username, u.name, u.role, "agent", self._effective(u, payload["profile"]), u.sources,
                             payload["jti"], payload.get("agent"), payload["profile"])
        if kind != "human":
            raise AuthError(401, "INVALID_TOKEN", "凭证类型无效")
        return Principal(u.username, u.name, u.role, "human", self._effective(u, None), u.sources, payload["jti"])

    def authenticate(self, token: str | None) -> Principal:
        if not token:
            raise AuthError(401, "UNAUTHENTICATED", "请先登录")
        return self._principal(self._decode(token))

    def demo_accounts(self) -> list[dict]:
        out = []
        for username, name, role, sources, pw in DEMO_USERS:
            scope = "全网" if not sources else "、".join(s.name for s in SOURCES if s.id in sources) + " 片区"
            out.append({"username": username, "name": name, "role": role, "role_name": ROLES[role][0],
                        "scope": scope, "password": pw})
        return out


def _local_iso(epoch: float) -> str:
    from .sim.history import TZ
    return datetime.fromtimestamp(epoch, TZ).replace(tzinfo=None).isoformat(timespec="seconds")
