"""SQLite 连接管理。

关键设计：
- 每次请求使用独立连接（FastAPI 的 sync 依赖在线程池执行），连接仅存活于单次操作；
- 写操作统一走 ``begin_immediate()``：立刻获取 RESERVED 锁，
  保证“读当前证据 -> 校验 -> 写入快照”整个临界区不会与其他写事务交错，
  从而避免并发更正与并发定版拼出不同提交状态的混合结果；
- 每个连接都开启外键约束、WAL 与 busy_timeout。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Literal

from .config import settings


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(
        db_path,
        timeout=settings.busy_timeout_ms / 1000.0,
        isolation_level=None,  # 事务完全由代码控制（autocommit 模式）
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {settings.busy_timeout_ms}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA synchronous = {settings.synchronous}")
    if settings.echo_sql:
        conn.set_trace_callback(lambda q: print(f"[SQL] {q}"))
    return conn


def init_db(db_path: str | None = None) -> None:
    """初始化数据库（幂等）：建目录、建表、开启 WAL。"""
    path = db_path or settings.db_path
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
    conn = _connect(path)
    try:
        conn.executescript(schema)
        # 全库只设置一次 WAL（:memory: 不支持，忽略报错）
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.DatabaseError:
            pass
    finally:
        conn.close()


TxMode = Literal["read", "immediate"]


@contextmanager
def get_conn(mode: TxMode = "read", db_path: str | None = None) -> Iterator[sqlite3.Connection]:
    """提供连接与事务边界。

    mode="immediate"：BEGIN IMMEDIATE，写操作专用，commit 成功后才释放锁；
    mode="read"：BEGIN（deferred 快照），用于需要读一致的多语句查询。
    """
    path = db_path or settings.db_path
    conn = _connect(path)
    try:
        if mode == "immediate":
            conn.execute("BEGIN IMMEDIATE")
        else:
            conn.execute("BEGIN")
        yield conn
        if mode == "immediate":
            conn.commit()
        else:
            conn.rollback()  # 只读事务直接结束即可
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
