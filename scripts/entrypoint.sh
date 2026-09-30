#!/usr/bin/env sh
# 容器入口：启动前完成数据库初始化（幂等），随后执行 CMD。
set -e

echo "[entrypoint] 初始化数据库: ${DB_PATH:-/data/allergens.db}"
python -m app.init_db

echo "[entrypoint] 启动服务: $*"
exec "$@"
