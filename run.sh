#!/usr/bin/env bash
# 同时启动后端（8000）与前端开发服务器（5173）
set -e
cd "$(dirname "$0")"
(cd backend && uvicorn app.main:app --port 8000) &
BACK=$!
trap 'kill $BACK 2>/dev/null' EXIT
cd frontend && npm run dev
