#!/usr/bin/env bash
# 一键启动牧场管理系统
set -e
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  python3 -m venv .venv 2>/dev/null || true
fi
if [ -x ".venv/bin/pip" ]; then
  .venv/bin/pip install -q -r requirements.txt
  PY=.venv/bin/python
else
  # 无 venv 的环境（如已 --user 安装依赖）
  python3 -c "import fastapi" 2>/dev/null || python3 -m pip install --user -r requirements.txt
  PY=python3
fi

# 数据库不存在时初始化样例库；已存在则保留生产数据（应用启动时会自动幂等迁移）
# 如需重新生成演示数据：RESET_DEMO=1 bash run.sh
if [ "${RESET_DEMO:-0}" = "1" ]; then
  $PY -m backend.seed
elif [ ! -f "backend/data/dairy.db" ]; then
  $PY -m backend.seed
fi

PORT="${PORT:-8000}"
echo "================================================"
echo "  牧场管理系统已启动： http://127.0.0.1:${PORT}"
echo "  API 文档（Swagger）： http://127.0.0.1:${PORT}/docs"
echo "  初始管理员： admin / admin123（首次登录请改密）"
echo "================================================"
exec $PY -m uvicorn backend.main:app --host 0.0.0.0 --port "${PORT}"
