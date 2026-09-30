"""应用配置：全部可通过环境变量覆盖（容器编排友好）。"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    # SQLite 数据库文件路径（:memory: 仅用于测试）
    db_path: str = os.environ.get("DB_PATH", "/data/allergens.db")
    # 服务监听地址与端口
    host: str = os.environ.get("HOST", "0.0.0.0")
    port: int = int(os.environ.get("PORT", "8080"))
    # 写事务等待锁的最长毫秒数
    busy_timeout_ms: int = int(os.environ.get("BUSY_TIMEOUT_MS", "10000"))
    # 是否每次写操作都 fsync（测试可关闭提速；默认安全优先）
    synchronous: str = os.environ.get("SQLITE_SYNC", "FULL")
    echo_sql: bool = _env_bool("ECHO_SQL", False)


settings = Settings()
