"""FastAPI 应用入口。

启动即完成数据库初始化（schema 幂等），容器编排可直接一键启动。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
import sqlite3

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import settings
from .database import init_db
from .errors import DomainError
from .routers import router


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 启动即完成初始化（schema 幂等），容器编排可直接一键启动
    init_db()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="批次过敏原声明追溯服务",
        version="1.0.0",
        lifespan=lifespan,
        description=(
            "食品加工厂批次级过敏原声明与来源追溯。\n\n"
            "- 原料过敏原集合按修订版本管理，更正采用乐观版本控制；\n"
            "- 批次引用原料/其他批次形成来源图，按全部可达原料当前版本取并集并给出路径；\n"
            "- 定版原子冻结声明，之后来源与声明不可改；\n"
            "- 查询已定版批次同时给出现算集合与差异，集合不同标记待复核。"
        ),
    )

    @app.exception_handler(DomainError)
    async def domain_error_handler(_: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.exception_handler(sqlite3.OperationalError)
    async def sqlite_locked_handler(_: Request, exc: sqlite3.OperationalError) -> JSONResponse:
        # 写锁等待超过 BUSY_TIMEOUT_MS：明确告知客户端可重试，而非 500
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            return JSONResponse(
                status_code=503,
                content={"error": {"code": "database_busy", "message": "数据库忙（写锁等待超时），请稍后重试"}},
            )
        raise exc

    app.include_router(router, prefix="/api/v1")
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    init_db()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        proxy_headers=True,
    )
