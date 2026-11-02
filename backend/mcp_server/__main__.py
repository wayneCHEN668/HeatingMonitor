"""启动 MCP 服务。

  HEATNET_TOKEN=<委托令牌> python -m mcp_server                    # stdio（Claude Desktop / Claude Code）
  python -m mcp_server --token <委托令牌> --transport streamable-http --port 8765

委托令牌在平台“智能体授权”页签发：令牌决定智能体身份、代表的用户、数据范围和可用工具
（智能体白名单 ∩ 用户权限）。热网后端需先运行（默认 http://127.0.0.1:8000）。
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from .client import ApiError, HeatnetAPI
from .server import build_server


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m mcp_server", description="热网监控系统 MCP 服务")
    p.add_argument("--api", default=os.environ.get("HEATNET_API", "http://127.0.0.1:8000"), help="热网后端地址")
    p.add_argument("--token", default=os.environ.get("HEATNET_TOKEN", ""),
                   help="智能体委托令牌（建议用环境变量 HEATNET_TOKEN 传入，避免出现在进程列表里）")
    p.add_argument("--model-label", default=os.environ.get("HEATNET_MODEL", ""),
                   help="接入方填写的模型/客户端版本，写入工具调用审计")
    p.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    a = p.parse_args(argv)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if not a.token:
        p.error("缺少委托令牌：请设置环境变量 HEATNET_TOKEN 或传入 --token（在平台“智能体授权”页签发）")
    try:
        server = build_server(HeatnetAPI(a.api, token=a.token), client_label=a.model_label)
    except (ApiError, ValueError) as e:
        print(f"heatnet MCP 启动失败：{getattr(e, 'message', e)}", file=sys.stderr)
        sys.exit(1)
    if a.transport == "stdio":
        print(f"heatnet MCP 已启动，后端 {a.api}", file=sys.stderr)
        server.run("stdio")
    else:
        server.run("streamable-http", host=a.host, port=a.port)


if __name__ == "__main__":
    main()
