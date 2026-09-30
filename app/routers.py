"""HTTP 路由。"""
from __future__ import annotations

from fastapi import APIRouter, status

from . import service
from .schemas import (
    BatchCreate,
    BatchOut,
    BatchSourcesUpdate,
    DeclarationOut,
    MaterialCorrect,
    MaterialCreate,
    MaterialOut,
    MaterialRevisionOut,
)

router = APIRouter()


# ---------- 健康检查 ----------


@router.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok"}


# ---------- 原料 ----------


@router.post("/materials", response_model=MaterialOut, status_code=status.HTTP_201_CREATED, tags=["materials"])
def create_material(payload: MaterialCreate) -> dict:
    return service.create_material(payload)


@router.get("/materials", response_model=list[MaterialOut], tags=["materials"])
def list_materials() -> list[dict]:
    return service.list_materials()


@router.get("/materials/{material_id}", response_model=MaterialOut, tags=["materials"])
def get_material(material_id: str) -> dict:
    return service.get_material(material_id)


@router.get("/materials/{material_id}/revisions", response_model=list[MaterialRevisionOut], tags=["materials"])
def get_material_revisions(material_id: str) -> list[dict]:
    return service.list_material_revisions(material_id)


@router.post("/materials/{material_id}/corrections", response_model=MaterialOut, tags=["materials"])
def correct_material(material_id: str, payload: MaterialCorrect) -> dict:
    """原料更正：携带期望版本，旧版本拒绝（409），历史版本保留。"""
    return service.correct_material(material_id, payload)


# ---------- 批次 ----------


@router.post("/batches", response_model=BatchOut, status_code=status.HTTP_201_CREATED, tags=["batches"])
def create_batch(payload: BatchCreate) -> dict:
    return service.create_batch(payload)


@router.get("/batches", response_model=list[BatchOut], tags=["batches"])
def list_batches() -> list[dict]:
    return service.list_batches()


@router.get("/batches/{batch_id}", response_model=BatchOut, tags=["batches"])
def get_batch(batch_id: str) -> dict:
    return service.get_batch(batch_id)


@router.put("/batches/{batch_id}/sources", response_model=BatchOut, tags=["batches"])
def update_sources(batch_id: str, payload: BatchSourcesUpdate) -> dict:
    """修改未定版批次的来源（全量替换）；循环/未知/空来源报 400，已定版报 409。"""
    return service.update_batch_sources(batch_id, payload)


@router.post("/batches/{batch_id}/finalize", response_model=BatchOut, tags=["batches"])
def finalize_batch(batch_id: str) -> dict:
    """定版：原子冻结声明集合、原料版本与来源路径；之后不可改。"""
    return service.finalize_batch(batch_id)


@router.get("/batches/{batch_id}/declaration", response_model=DeclarationOut, tags=["batches"])
def get_declaration(batch_id: str) -> dict:
    """过敏原声明：定版批次同时返回冻结快照、当前现算结果与差异（added/removed）。"""
    return service.get_declaration(batch_id)
