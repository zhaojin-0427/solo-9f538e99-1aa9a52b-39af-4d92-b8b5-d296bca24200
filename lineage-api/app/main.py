"""FastAPI entry point for the lineage impact analysis service."""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import service
from .db import init_db
from .exceptions import LineageError
from .models import (
    GraphResponse,
    ImpactRequest,
    ImpactResponse,
    RegisterRequest,
    RegisterResponse,
    UndoResponse,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Reads db.DEFAULT_DB_PATH at startup so tests can override it.
    init_db()
    yield


app = FastAPI(
    title="数据血缘影响分析 API",
    description=(
        "批量登记源字段 / 派生字段 / 报表列依赖，提交拟下线字段后递归分析"
        "确实失效的报表列及各候选组缺失依赖；成功批次可按 batch_id 撤销。"
    ),
    version="1.1.0",
    lifespan=lifespan,
)


@app.exception_handler(LineageError)
async def _lineage_error_handler(
    _request: Request, exc: LineageError
) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": exc.code,
            "message": exc.message,
            "details": exc.details,
        },
    )


@app.exception_handler(sqlite3.OperationalError)
async def _sqlite_error_handler(
    _request: Request, exc: sqlite3.OperationalError
) -> JSONResponse:
    if "locked" in str(exc).lower():
        return JSONResponse(
            status_code=503,
            content={
                "error": "database_busy",
                "message": "数据库正被登记事务占用，请重试",
            },
        )
    raise exc


@app.get("/health", tags=["meta"])
def health() -> dict:
    from .db import DEFAULT_DB_PATH

    return {"status": "ok", "db": DEFAULT_DB_PATH}


@app.post(
    "/lineage/register",
    response_model=RegisterResponse,
    tags=["lineage"],
    status_code=201,
)
def register(payload: RegisterRequest) -> dict:
    """批量登记 / 覆盖血缘；循环依赖或校验失败时整批不生效。"""
    return service.register_batch(payload.nodes)


@app.post(
    "/lineage/batches/{batch_id}/undo",
    response_model=UndoResponse,
    tags=["lineage"],
)
def undo(batch_id: int) -> dict:
    """按批次号撤销一批登记。

    - 恢复该批覆盖字段的原定义；
    - 删除该批新增且未被后续批次引用的字段；
    - 若后续批次修改过这些字段或引用该批新增字段，返回 409 且血缘不变；
    - 重复撤销返回首次成功结果。
    """
    return service.undo_batch(batch_id)


@app.post(
    "/lineage/impact",
    response_model=ImpactResponse,
    tags=["lineage"],
)
def impact(payload: ImpactRequest) -> dict:
    """递归分析拟下线字段导致的失效报表列。"""
    return service.analyze_impact(payload.retiring)


@app.get("/lineage/graph", response_model=GraphResponse, tags=["lineage"])
def graph() -> dict:
    """查看当前已提交的完整血缘快照。"""
    return service.get_graph()
