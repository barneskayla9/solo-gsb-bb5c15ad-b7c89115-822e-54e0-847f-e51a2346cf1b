"""独立初始化入口：python -m app.init_db

创建数据目录、表结构并开启 WAL；重复执行安全（CREATE TABLE IF NOT EXISTS）。
"""
from __future__ import annotations

from .config import settings
from .database import init_db


def main() -> None:
    print(f"[init_db] DB_PATH = {settings.db_path}")
    init_db()
    print("[init_db] 数据库初始化完成（表、索引、WAL 均已就绪）")


if __name__ == "__main__":
    main()
