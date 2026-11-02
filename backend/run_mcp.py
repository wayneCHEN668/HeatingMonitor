"""MCP 服务启动脚本：可用绝对路径直接运行，便于在 Claude Desktop / Claude Code 等客户端中配置。

  python /绝对路径/backend/run_mcp.py --api http://127.0.0.1:8000
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp_server.__main__ import main  # noqa: E402

if __name__ == "__main__":
    main()
