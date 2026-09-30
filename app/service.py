"""业务服务层。

所有写操作均在 BEGIN IMMEDIATE 事务内完成：
事务开始即获取写锁，同一连接内完成“读当前状态 -> 图校验 -> 落库”，
杜绝并发更正与并发定版拼接出不同提交状态的中间结果。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from collections import defaultdict

from .database import get_conn
from .errors import AlreadyFinalized, Conflict, NotFound, VersionConflict, ValidationFailure
from .graph import Graph, paths_for_json, traverse
from .schemas import SourceRef, normalize_allergens


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# ============================================================
# 数据加载（必须在调用方已开启的事务连接上执行）
# ============================================================


def _load_graph(conn: sqlite3.Connection) -> Graph:
    """在当前事务内快照加载整张来源图（中小规模食品厂场景足够）。

    用 LEFT JOIN 保留“当前版本过敏原集合为空”的原料，
    否则空集原料会从图中整体消失并污染上游路径（相当于未知原料）。
    """
    materials: dict[str, dict] = {}
    mat_rows = conn.execute(
        """
        SELECT m.id AS id, m.current_rev AS rev, a.allergen AS allergen
        FROM materials m
        LEFT JOIN material_revision_allergens a
          ON a.material_id = m.id AND a.revision = m.current_rev
        """
    ).fetchall()
    for row in mat_rows:
        bucket = materials.setdefault(row["id"], {"revision": row["rev"], "allergens": set()})
        if row["allergen"] is not None:
            bucket["allergens"].add(row["allergen"])

    batches: dict[str, dict] = {}
    batch_rows = conn.execute("SELECT id, finalized FROM batches").fetchall()
    for row in batch_rows:
        batches[row["id"]] = {"finalized": bool(row["finalized"]), "sources": []}
    src_rows = conn.execute(
        "SELECT batch_id, ref_type, ref_id FROM batch_sources ORDER BY batch_id, position"
    ).fetchall()
    for row in src_rows:
        batches[row["batch_id"]]["sources"].append((row["ref_type"], row["ref_id"]))

    return Graph(materials=materials, batches=batches)


def _material_allergens(conn: sqlite3.Connection, material_id: str, revision: int) -> list[str]:
    rows = conn.execute(
        "SELECT allergen FROM material_revision_allergens WHERE material_id = ? AND revision = ? ORDER BY allergen",
        (material_id, revision),
    ).fetchall()
    return [r["allergen"] for r in rows]


def _get_material_or_404(conn: sqlite3.Connection, material_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM materials WHERE id = ?", (material_id,)).fetchone()
    if row is None:
        raise NotFound(f"原料不存在: {material_id}")
    return row


def _get_batch_or_404(conn: sqlite3.Connection, batch_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if row is None:
        raise NotFound(f"批次不存在: {batch_id}")
    return row


def _batch_sources(conn: sqlite3.Connection, batch_id: str) -> list[SourceRef]:
    rows = conn.execute(
        "SELECT ref_type, ref_id FROM batch_sources WHERE batch_id = ? ORDER BY position",
        (batch_id,),
    ).fetchall()
    return [SourceRef(type=r["ref_type"], id=r["ref_id"]) for r in rows]


def _dedup_sources(sources: list[SourceRef]) -> list[SourceRef]:
    """按 (type, id) 去重并保留首次出现的顺序。

    需求规定“重复来源只计一次”，因此同一引用在请求中重复出现时静默去重，
    不视为错误（过敏原路径在遍历层同样按集合去重）。
    """
    seen: set[tuple[str, str]] = set()
    unique: list[SourceRef] = []
    for ref in sources:
        key = (ref.type, ref.id)
        if key not in seen:
            seen.add(key)
            unique.append(ref)
    return unique


def _validate_sources(graph: Graph, batch_id: str, sources: list[SourceRef]) -> None:
    """设置来源前的全部校验：非空、引用存在、无环（含自引用）。"""
    if not sources:
        raise ValidationFailure(
            f"批次 {batch_id} 来源不能为空", code="empty_sources"
        )
    for ref in sources:
        if ref.type == "material" and ref.id not in graph.materials:
            raise ValidationFailure(f"未知原料来源: {ref.id}", code="unknown_source")
        if ref.type == "batch":
            if ref.id not in graph.batches:
                raise ValidationFailure(f"未知批次来源: {ref.id}", code="unknown_source")
    # 用“假设替换后”的图做一次完整遍历，捕获环与可达路径上的空来源
    trial = Graph(materials=graph.materials, batches=graph.batches)
    trial.batches = dict(graph.batches)
    trial.batches[batch_id] = {
        "finalized": graph.batches[batch_id]["finalized"],
        "sources": [(r.type, r.id) for r in sources],
    }
    traverse(trial, batch_id, require_sources=True)


# ============================================================
# 原料
# ============================================================


def create_material(data) -> dict:
    allergens = normalize_allergens(data.allergens)
    with get_conn(mode="immediate") as conn:
        material_id = data.id or _new_id("M")
        exists = conn.execute("SELECT 1 FROM materials WHERE id = ?", (material_id,)).fetchone()
        if exists:
            raise Conflict(f"原料 ID 已存在: {material_id}", code="id_conflict")
        conn.execute(
            "INSERT INTO materials (id, name, current_rev) VALUES (?, ?, 1)",
            (material_id, data.name.strip()),
        )
        conn.execute(
            "INSERT INTO material_revisions (material_id, revision, note) VALUES (?, 1, ?)",
            (material_id, "创建"),
        )
        conn.executemany(
            "INSERT INTO material_revision_allergens (material_id, revision, allergen) VALUES (?, 1, ?)",
            [(material_id, a) for a in allergens],
        )
        row = _get_material_or_404(conn, material_id)
        return _material_out(conn, row)


def list_materials() -> list[dict]:
    with get_conn(mode="read") as conn:
        rows = conn.execute("SELECT * FROM materials ORDER BY id").fetchall()
        return [_material_out(conn, r) for r in rows]


def get_material(material_id: str) -> dict:
    with get_conn(mode="read") as conn:
        row = _get_material_or_404(conn, material_id)
        return _material_out(conn, row)


def list_material_revisions(material_id: str) -> list[dict]:
    with get_conn(mode="read") as conn:
        mat = _get_material_or_404(conn, material_id)
        rows = conn.execute(
            """
            SELECT revision, created_at
              FROM material_revisions
             WHERE material_id = ?
             ORDER BY revision
            """,
            (material_id,),
        ).fetchall()
        result = []
        for r in rows:
            rev = r["revision"]
            result.append(
                {
                    "revision": rev,
                    "allergens": _material_allergens(conn, material_id, rev),
                    "created_at": r["created_at"],
                }
            )
        # 原料允许初版无过敏原（revision 1 但无 allergen 行），用原料创建时间补占位
        if not result:
            result.append({"revision": 1, "allergens": [], "created_at": mat["created_at"]})
        return result


def correct_material(material_id: str, data) -> dict:
    """原料更正（乐观并发控制）。

    - expected_revision 必须等于当前版本，否则 409 version_conflict（旧版本拒绝）；
    - 更正以新版本追加，历史修订行永久保留；
    - 整个操作在 IMMEDIATE 事务内：并发更正串行化，后到的一方会因版本不匹配被拒，
      与并发定版也不会互相读到半提交状态。
    """
    allergens = normalize_allergens(data.allergens)
    with get_conn(mode="immediate") as conn:
        row = _get_material_or_404(conn, material_id)
        current = row["current_rev"]
        if data.expected_revision != current:
            raise VersionConflict(
                f"原料 {material_id} 当前版本为 {current}，提交基于版本 {data.expected_revision}，已拒绝；"
                f"请基于最新版本重新提交",
            )
        new_rev = current + 1
        conn.execute(
            "INSERT INTO material_revisions (material_id, revision, note) VALUES (?, ?, ?)",
            (material_id, new_rev, data.note),
        )
        conn.executemany(
            "INSERT INTO material_revision_allergens (material_id, revision, allergen) VALUES (?, ?, ?)",
            [(material_id, new_rev, a) for a in allergens],
        )
        conn.execute(
            "UPDATE materials SET current_rev = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
            (new_rev, material_id),
        )
        row = _get_material_or_404(conn, material_id)
        return _material_out(conn, row)


def _material_out(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "current_revision": row["current_rev"],
        "allergens": _material_allergens(conn, row["id"], row["current_rev"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


# ============================================================
# 批次
# ============================================================


def create_batch(data) -> dict:
    sources = _dedup_sources(data.sources)
    with get_conn(mode="immediate") as conn:
        batch_id = data.id or _new_id("B")
        if conn.execute("SELECT 1 FROM batches WHERE id = ?", (batch_id,)).fetchone():
            raise Conflict(f"批次 ID 已存在: {batch_id}", code="id_conflict")
        graph = _load_graph(conn)
        # 新批次先放进图（空来源），再按提交内容校验
        graph.batches[batch_id] = {"finalized": False, "sources": []}
        if sources:
            _validate_sources(graph, batch_id, sources)
        conn.execute(
            "INSERT INTO batches (id, name) VALUES (?, ?)",
            (batch_id, data.name.strip()),
        )
        _insert_sources(conn, batch_id, sources)
        row = _get_batch_or_404(conn, batch_id)
        return _batch_out(conn, row)


def list_batches() -> list[dict]:
    with get_conn(mode="read") as conn:
        rows = conn.execute("SELECT * FROM batches ORDER BY id").fetchall()
        return [_batch_out(conn, r) for r in rows]


def get_batch(batch_id: str) -> dict:
    with get_conn(mode="read") as conn:
        row = _get_batch_or_404(conn, batch_id)
        return _batch_out(conn, row)


def update_batch_sources(batch_id: str, data) -> dict:
    """未定版批次可改来源；全量替换，重复/空/未知/循环均报错。"""
    sources = _dedup_sources(data.sources)
    with get_conn(mode="immediate") as conn:
        row = _get_batch_or_404(conn, batch_id)
        if row["finalized"]:
            raise AlreadyFinalized(
                f"批次 {batch_id} 已定版（{row['finalized_at']}），来源不可修改"
            )
        graph = _load_graph(conn)
        _validate_sources(graph, batch_id, sources)
        conn.execute("DELETE FROM batch_sources WHERE batch_id = ?", (batch_id,))
        _insert_sources(conn, batch_id, sources)
        conn.execute(
            "UPDATE batches SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
            (batch_id,),
        )
        row = _get_batch_or_404(conn, batch_id)
        return _batch_out(conn, row)


def finalize_batch(batch_id: str) -> dict:
    """定版：原子保存声明集合、来源原料版本与每条证据路径。

    定版后该批次来源与声明均不可改。定版瞬间在单个 IMMEDIATE 事务内：
    遍历当前图 -> 写 frozen_declarations -> 置 finalized=1，
    任何并发原料更正要么整个先于此事务提交（其新版本被本次定版看到），
    要么整个后于此事务（被快照排除），不存在混合状态。
    """
    with get_conn(mode="immediate") as conn:
        row = _get_batch_or_404(conn, batch_id)
        if row["finalized"]:
            raise AlreadyFinalized(f"批次 {batch_id} 已经定版，不可重复定版")
        graph = _load_graph(conn)
        result = traverse(graph, batch_id, require_sources=True)

        rows_to_insert = []
        for allergen, paths in result.allergen_paths.items():
            for path in paths:
                material_id = path[-1]
                rev = result.material_revisions[material_id]
                rows_to_insert.append((batch_id, material_id, rev, allergen, json.dumps(list(path))))
        conn.executemany(
            """
            INSERT OR REPLACE INTO frozen_declarations
                (batch_id, material_id, material_revision, allergen, path_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            rows_to_insert,
        )
        conn.execute(
            """
            UPDATE batches
               SET finalized = 1,
                   finalized_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                   updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
             WHERE id = ?
            """,
            (batch_id,),
        )
        row = _get_batch_or_404(conn, batch_id)
        return _batch_out(conn, row)


def _insert_sources(conn: sqlite3.Connection, batch_id: str, sources: list[SourceRef]) -> None:
    conn.executemany(
        "INSERT INTO batch_sources (batch_id, position, ref_type, ref_id) VALUES (?, ?, ?, ?)",
        [(batch_id, pos, ref.type, ref.id) for pos, ref in enumerate(sources)],
    )


def _batch_out(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "finalized": bool(row["finalized"]),
        "finalized_at": row["finalized_at"],
        "sources": [s.model_dump() for s in _batch_sources(conn, row["id"])],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


# ============================================================
# 过敏原声明查询
# ============================================================


def get_declaration(batch_id: str) -> dict:
    """返回批次声明。

    - 未定版：仅返回按当前证据现算的集合与路径；
    - 已定版：同时返回定版冻结快照与现算结果，给出 added/removed；
      集合不一致时 needs_review=true（待复核）。
    下游已定版批次同样按当前证据重算（其冻结构不变）。
    """
    with get_conn(mode="read") as conn:
        row = _get_batch_or_404(conn, batch_id)
        graph = _load_graph(conn)
        finalized = bool(row["finalized"])

        # 现算：定版图中可达的已定版批次也是普通图节点，按其当前来源边继续向上追溯
        try:
            result = traverse(graph, batch_id, require_sources=not finalized)
            current = {
                "allergens": [
                    {
                        "allergen": a,
                        "paths": paths_for_json(paths),
                    }
                    for a, paths in sorted(result.allergen_paths.items())
                ],
                "material_versions": dict(sorted(result.material_revisions.items())),
            }
        except ValidationFailure as exc:
            # 定版后上游图可能变为异常状态（数据问题），不阻断快照查询
            if not finalized:
                raise
            current = {"error": exc.message, "code": exc.code, "allergens": [], "material_versions": {}}

        frozen = None
        added: list[str] = []
        removed: list[str] = []
        needs_review = False

        if finalized:
            frozen_rows = conn.execute(
                "SELECT material_id, material_revision, allergen, path_json FROM frozen_declarations WHERE batch_id = ?",
                (batch_id,),
            ).fetchall()
            frozen = _frozen_block(frozen_rows)
            frozen_set = {e["allergen"] for e in frozen["allergens"]}
            current_set = {e["allergen"] for e in current["allergens"]}
            added = sorted(current_set - frozen_set)
            removed = sorted(frozen_set - current_set)
            needs_review = bool(added or removed)

        return {
            "batch_id": batch_id,
            "finalized": finalized,
            "frozen": frozen,
            "current": current,
            "added": added,
            "removed": removed,
            "needs_review": needs_review,
        }


def _frozen_block(frozen_rows: list[sqlite3.Row]) -> dict:
    evidence: dict[str, set[tuple[str, ...]]] = defaultdict(set)
    versions: dict[str, int] = {}
    for r in frozen_rows:
        evidence[r["allergen"]].add(tuple(json.loads(r["path_json"])))
        versions[r["material_id"]] = r["material_revision"]
    return {
        "allergens": [
            {"allergen": a, "paths": paths_for_json(paths)}
            for a, paths in sorted(evidence.items())
        ],
        "material_versions": dict(sorted(versions.items())),
    }
