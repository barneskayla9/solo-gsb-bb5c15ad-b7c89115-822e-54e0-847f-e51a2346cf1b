"""来源图遍历引擎。

规则（需求对应）：
- 批次引用原料或其他批次，形成有向来源图 batch -> material|batch；
- 沿图取“全部可达原料的当前版本”过敏原并集；
- 每个过敏原返回其来源路径（根批次 -> ... -> 原料），重复路径只计一次；
- 存在环（沿当前来源边可回到当前 DFS 栈上的批次）时报循环引用；
- 引用了不存在的原料/批次时报未知来源；
- 批次来源为空时报空来源（定版校验时）。

实现为纯函数，数据由 service 层在单个事务内快照加载，
保证遍历看到的图与数据库提交状态一致。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .errors import ValidationFailure


@dataclass
class Graph:
    # material_id -> {"revision": int, "allergens": set[str]}
    materials: dict[str, dict]
    # batch_id -> {"finalized": bool, "sources": [(type, id), ...]}
    batches: dict[str, dict]


@dataclass
class TraversalResult:
    # allergen -> 去重后的路径集合；路径为节点 id 列表，末位是原料 id
    allergen_paths: dict[str, set[tuple[str, ...]]] = field(default_factory=dict)
    # material_id -> 当前版本（被采用作为证据的版本）
    material_revisions: dict[str, int] = field(default_factory=dict)


def traverse(
    graph: Graph,
    root_batch_id: str,
    *,
    require_sources: bool = True,
) -> TraversalResult:
    """从根批次出发计算当前过敏原证据。

    require_sources=True 时，任何可达的未定版/已定版批次（含根）来源为空均报错；
    原始查询中我们对根批次同样要求至少一个来源，避免“空声明”被静默接受。
    """
    if root_batch_id not in graph.batches:
        raise ValidationFailure(f"批次不存在: {root_batch_id}", code="not_found", status_code=404)

    result = TraversalResult()
    stack: list[str] = []          # 当前 DFS 栈（有序），用于还原环路径
    on_stack: set[str] = set()    # 栈的快速查询集合
    memo: dict[str, dict[str, set[tuple[str, ...]]]] = {}

    def walk(batch_id: str) -> dict[str, set[tuple[str, ...]]]:
        # 先做环检测再查 memo：环上的节点会先抛错，永远不会进入 memo。
        if batch_id in on_stack:
            start = stack.index(batch_id)
            cycle = stack[start:] + [batch_id]
            raise ValidationFailure(
                f"检测到循环引用: {' -> '.join(cycle)}", code="cyclic_reference"
            )

        if batch_id in memo:
            return memo[batch_id]

        batch = graph.batches.get(batch_id)
        if batch is None:
            # 理论上引用存在性在加载阶段已校验，这里是防御性分支
            raise ValidationFailure(f"未知批次来源: {batch_id}", code="unknown_source")

        sources = batch["sources"]
        if require_sources and not sources:
            if batch_id == root_batch_id:
                raise ValidationFailure(
                    f"批次 {batch_id} 没有任何来源，无法形成过敏原声明",
                    code="empty_sources",
                )
            raise ValidationFailure(
                f"批次 {batch_id} 的来源为空（不可作为上游）",
                code="empty_sources",
            )

        on_stack.add(batch_id)
        stack.append(batch_id)
        # 本节点产出的“相对路径”：allergen -> { (本批次, ..., 原料), ... }
        contributed: dict[str, set[tuple[str, ...]]] = {}
        try:
            for ref_type, ref_id in sources:
                if ref_type == "material":
                    material = graph.materials.get(ref_id)
                    if material is None:
                        raise ValidationFailure(
                            f"批次 {batch_id} 引用了未知原料: {ref_id}",
                            code="unknown_source",
                        )
                    rev = material["revision"]
                    result.material_revisions.setdefault(ref_id, rev)
                    for allergen in material["allergens"]:
                        contributed.setdefault(allergen, set()).add((batch_id, ref_id))
                else:
                    if ref_id not in graph.batches:
                        raise ValidationFailure(
                            f"批次 {batch_id} 引用了未知批次: {ref_id}",
                            code="unknown_source",
                        )
                    sub = walk(ref_id)
                    for allergen, sub_paths in sub.items():
                        bucket = contributed.setdefault(allergen, set())
                        for p in sub_paths:
                            bucket.add((batch_id, *p))
        finally:
            on_stack.discard(batch_id)
            stack.pop()

        memo[batch_id] = contributed
        return contributed

    root_paths = walk(root_batch_id)

    # 根路径即为绝对路径（从根批次到原料），直接收集
    for allergen, paths in root_paths.items():
        result.allergen_paths.setdefault(allergen, set()).update(paths)

    return result


# ---------- 结果整理工具 ----------


def paths_for_json(paths: set[tuple[str, ...]]) -> list[list[str]]:
    """路径元组转 JSON：按路径长度、再按字典序稳定排序。"""
    return [list(p) for p in sorted(paths, key=lambda p: (len(p), p))]


def union_allergens(evidence: dict[str, set[tuple[str, ...]]]) -> list[str]:
    return sorted(evidence)
