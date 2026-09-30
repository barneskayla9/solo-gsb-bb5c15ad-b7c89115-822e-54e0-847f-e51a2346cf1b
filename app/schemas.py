"""HTTP 请求/响应模型（Pydantic v2）。"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")

SourceType = Literal["material", "batch"]


def _validate_id(value: str) -> str:
    value = value.strip()
    if not ID_PATTERN.match(value):
        raise ValueError(
            "id 必须为 1-64 个字符，只能包含字母、数字、下划线、点、冒号、连字符，且以字母或数字开头"
        )
    return value


def normalize_allergens(values: list[str]) -> list[str]:
    """过敏原规范化：去空白、小写、去空串、去重并排序。"""
    seen: set[str] = set()
    result: list[str] = []
    for raw in values or []:
        item = (raw or "").strip().lower()
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return sorted(seen)


class SourceRef(BaseModel):
    type: SourceType
    id: str

    @field_validator("id")
    @classmethod
    def _id_ok(cls, v: str) -> str:
        return _validate_id(v)


class MaterialCreate(BaseModel):
    id: str | None = Field(default=None, description="原料 ID；缺省自动生成 M-xxxx")
    name: str = Field(min_length=1, max_length=200)
    allergens: list[str] = Field(default_factory=list, description="过敏原名称，自动小写去重")


class MaterialCorrect(BaseModel):
    expected_revision: int = Field(gt=0, description="客户端看到的当前版本；不匹配则拒绝（乐观锁）")
    allergens: list[str] = Field(description="更正后的完整过敏原集合（全量替换）")
    note: str | None = Field(default=None, max_length=500)


class BatchCreate(BaseModel):
    id: str | None = Field(default=None, description="批次 ID；缺省自动生成 B-xxxx")
    name: str = Field(min_length=1, max_length=200)
    sources: list[SourceRef] = Field(default_factory=list, description="初始来源（可为空，未定版可后改）")


class BatchSourcesUpdate(BaseModel):
    sources: list[SourceRef] = Field(description="完整来源列表（全量替换）；不允许空/未知/循环")


# ---------- 响应模型 ----------


class MaterialOut(BaseModel):
    id: str
    name: str
    current_revision: int
    allergens: list[str]
    created_at: str
    updated_at: str


class MaterialRevisionOut(BaseModel):
    revision: int
    allergens: list[str]
    created_at: str | None = None


class BatchOut(BaseModel):
    id: str
    name: str
    finalized: bool
    finalized_at: str | None
    sources: list[SourceRef]
    created_at: str
    updated_at: str


class AllergenEvidence(BaseModel):
    allergen: str
    paths: list[list[str]]  # 去重后的完整来源路径（根批次 -> ... -> 原料）


class DeclarationOut(BaseModel):
    batch_id: str
    finalized: bool
    # 定版时冻结的声明（未定版为 None）
    frozen: dict | None = None
    # 按当前证据现算的声明
    current: dict
    # 现算集合相对定版集合的差异
    added: list[str] = []
    removed: list[str] = []
    needs_review: bool = False
