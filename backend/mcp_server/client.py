"""热网后端 REST API 的同步客户端。MCP 工具层只通过它访问系统，不直接触碰仿真/控制内存状态。"""
from __future__ import annotations

from typing import Any

import httpx


class ApiError(Exception):
    def __init__(self, message: str, code: str = "API_ERROR", retryable: bool = False, status: int | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.retryable = retryable
        self.status = status


class HeatnetAPI:
    """封装 /api/*。所有方法返回后端封套里的 data，或在失败时抛出 ApiError。"""

    def __init__(self, base_url: str = "http://127.0.0.1:8000", token: str = "",
                 client: httpx.Client | None = None, timeout: float = 60):
        self.http = client or httpx.Client(base_url=base_url, timeout=timeout)
        self.token = token

    def _req(self, method: str, path: str, **kw) -> dict:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        try:
            r = self.http.request(method, path, headers=headers, **kw)
        except httpx.TimeoutException as e:
            raise ApiError(f"后端响应超时：{e}", "TIMEOUT", retryable=True) from e
        except httpx.TransportError as e:
            raise ApiError(f"无法连接热网后端（{self.http.base_url}）：{e}", "UNAVAILABLE", retryable=True) from e
        try:
            body = r.json()
        except ValueError as e:
            raise ApiError(f"后端返回非 JSON（HTTP {r.status_code}）", "BAD_RESPONSE", status=r.status_code) from e
        if r.status_code == 401:
            err = body.get("error") or {}
            raise ApiError(f"委托令牌无效：{err.get('message', '未认证')}。请在平台“智能体授权”页重新签发",
                           err.get("code", "UNAUTHENTICATED"), status=401)
        if r.status_code >= 400 or (isinstance(body, dict) and body.get("ok") is False):
            err = body.get("error") or {}
            msg = err.get("message") or body.get("summary") or body.get("detail") or f"HTTP {r.status_code}"
            code = err.get("code") or ("NOT_FOUND" if r.status_code == 404 else "API_ERROR")
            raise ApiError(str(msg), code, retryable=r.status_code >= 500, status=r.status_code)
        return body

    def get(self, path: str, **params) -> Any:
        return self._req("GET", path, params={k: v for k, v in params.items() if v is not None})["data"]

    def get_env(self, path: str, **params) -> dict:
        """返回完整封套（AI 接口自带 summary / evidence / warnings）。"""
        return self._req("GET", path, params={k: v for k, v in params.items() if v is not None})

    def post(self, path: str, body: dict | None = None) -> dict:
        return self._req("POST", path, json=body or {})
