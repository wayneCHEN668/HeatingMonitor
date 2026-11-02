"""启动 MCP 服务。

  python -m mcp_server                                   # stdio（Claude Desktop / Claude Code）
  python -m mcp_server --transport streamable-http --port 8765
  python -m mcp_server --profile analyst                 # 只读分析智能体，不加载 control_* 工具

热网后端需先运行（默认 http://127.0.0.1:8000，可用 --api 或环境变量 HEATNET_API 指定）。
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from .client import HeatnetAPI
from .server import PROFILES, build_server


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m mcp_server", description="热网监控系统 MCP 服务")
    p.add_argument("--api", default=os.environ.get("HEATNET_API", "http://127.0.0.1:8000"), help="热网后端地址")
    p.add_argument("--profile", choices=list(PROFILES), default=os.environ.get("HEATNET_PROFILE", "dispatch"),
                   help="dispatch=调度智能体（含调控工具）；analyst=只读分析")
    p.add_argument("--agent", default=os.environ.get("HEATNET_AGENT", "调度智能体"), help="智能体名称（写入审计）")
    p.add_argument("--user", default=os.environ.get("HEATNET_USER", "调度员"), help="代表的用户（写入审计）")
    p.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    a = p.parse_args(argv)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    server = build_server(HeatnetAPI(a.api), profile=a.profile, agent=a.agent, user=a.user)
    if a.transport == "stdio":
        print(f"heatnet MCP（{a.profile}）已启动，后端 {a.api}", file=sys.stderr)
        server.run("stdio")
    else:
        server.run("streamable-http", host=a.host, port=a.port)


if __name__ == "__main__":
    main()
