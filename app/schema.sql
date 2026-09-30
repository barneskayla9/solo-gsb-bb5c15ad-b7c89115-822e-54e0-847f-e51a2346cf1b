-- 批次过敏原声明追溯服务 数据库结构
-- SQLite (WAL)，所有写操作在 IMMEDIATE 事务内完成，保证原子提交。

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- 原料主记录：过敏原集合以修订版本（revision）形式追加保存，历史永不删除。
CREATE TABLE IF NOT EXISTS materials (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    current_rev INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 原料修订版本头：每次创建/更正好产生一个新版本行，即使过敏原集合为空也保留。
-- 历史永久追加，永不更新或删除。
CREATE TABLE IF NOT EXISTS material_revisions (
    material_id TEXT NOT NULL REFERENCES materials(id),
    revision    INTEGER NOT NULL,
    note        TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (material_id, revision)
);

-- 每个修订版本下的过敏原条目（空集合时该表无对应行，但版本头仍在）。
CREATE TABLE IF NOT EXISTS material_revision_allergens (
    material_id TEXT NOT NULL,
    revision    INTEGER NOT NULL,
    allergen    TEXT NOT NULL,                 -- 规范化后的过敏原（小写、去空白）
    PRIMARY KEY (material_id, revision, allergen),
    FOREIGN KEY (material_id, revision) REFERENCES material_revisions(material_id, revision)
);

-- 生产批次：finalized=1 后来源与声明均不可修改。
CREATE TABLE IF NOT EXISTS batches (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    finalized    INTEGER NOT NULL DEFAULT 0,
    finalized_at TEXT,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 批次来源边：批次 -> 原料或其他批次（定版后不可变）。
CREATE TABLE IF NOT EXISTS batch_sources (
    batch_id  TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    position  INTEGER NOT NULL,                -- 来源在提交中的顺序，用于稳定路径展示
    ref_type  TEXT NOT NULL CHECK (ref_type IN ('material', 'batch')),
    ref_id    TEXT NOT NULL,
    PRIMARY KEY (batch_id, ref_type, ref_id)
);

CREATE INDEX IF NOT EXISTS idx_batch_sources_ref ON batch_sources(ref_type, ref_id);

-- 定版快照：定版瞬间按当时全部可达原料当前版本冻结的声明证据。
-- 每个 (原料版本, 过敏原, 来源路径) 一行，定版原子写入。
CREATE TABLE IF NOT EXISTS frozen_declarations (
    batch_id           TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    material_id        TEXT NOT NULL,
    material_revision  INTEGER NOT NULL,
    allergen           TEXT NOT NULL,
    path_json          TEXT NOT NULL,          -- 完整来源路径，如 ["B2","B1","M1"]
    PRIMARY KEY (batch_id, material_id, material_revision, allergen, path_json)
);

CREATE INDEX IF NOT EXISTS idx_frozen_batch ON frozen_declarations(batch_id);
