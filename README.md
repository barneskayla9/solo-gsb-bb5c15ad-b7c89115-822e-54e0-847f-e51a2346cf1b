# 批次过敏原声明追溯服务

食品加工厂批次级**过敏原声明（allergen declaration）与来源追溯** HTTP 服务。

- **技术栈**：Python 3.11 · FastAPI · SQLite（WAL 模式）· Uvicorn
- 原料记录过敏原集合与**修订版本**；生产批次引用原料或其他批次，形成有向**来源图**
- 按全部可达原料的**当前版本**取过敏原并集，为每项过敏原返回完整**来源路径**（重复来源只计一次）
- 原料更正携带**期望版本**（乐观锁）：版本不符一律拒绝，历史修订永久保留
- 未定版批次可修改来源；循环引用、未知来源、空来源均报错
- **定版**时原子保存声明集合、来源原料版本与每条证据路径；定版后来源与声明不可改
- 上游原料更正后，查询已定版批次同时返回：定版冻结集合、按当前证据的现算集合、**新增/消失过敏原**，集合不一致标记 `needs_review`（待复核）；下游批次同样按当前证据重算
- 所有写操作走 `BEGIN IMMEDIATE` 单事务，并发更正与并发定版**不会拼接出跨提交的混合状态**

---

## 1. 一键启动（Docker Compose，推荐）

```bash
docker compose up --build
```

启动后：

- 服务地址：**http://localhost:8080**
- API 前缀：**`/api/v1`**
- 健康检查：http://localhost:8080/api/v1/health
- 交互式 API 文档（Swagger UI）：http://localhost:8080/docs
- OpenAPI 描述：http://localhost:8080/openapi.json

容器入口 `scripts/entrypoint.sh` 会在服务启动前自动执行数据库初始化（建表、索引、开启 WAL），**无需额外初始化步骤**。SQLite 数据保存在命名卷 `allergen_data`（容器内 `/data/allergens.db`）。

停止并清理：

```bash
docker compose down          # 停止（数据卷保留）
docker compose down -v       # 停止并删除数据卷
```

### 仅用 Docker

```bash
docker build -t allergen-traceability:1.0.0 .
docker run --rm -p 8080:8080 -v allergen_data:/data allergen-traceability:1.0.0
```

## 2. 本地直接运行（无 Docker）

需要 Python 3.11+：

```bash
pip install -r requirements.txt

# 1) 初始化数据库（幂等，可重复执行）
DB_PATH=./data/allergens.db python -m app.init_db

# 2) 启动服务
DB_PATH=./data/allergens.db uvicorn app.main:app --host 0.0.0.0 --port 8080
```

## 3. 配置项（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DB_PATH` | `/data/allergens.db` | SQLite 数据库文件路径，父目录自动创建 |
| `HOST` | `0.0.0.0` | 监听地址 |
| `PORT` | `8080` | 监听端口（Compose 宿主端口可用同名变量映射：`PORT=18080 docker compose up`） |
| `BUSY_TIMEOUT_MS` | `10000` | 写锁等待上限（毫秒），超时返回 409 |
| `SQLITE_SYNC` | `FULL` | SQLite synchronous 级别（`FULL`/`NORMAL`/`OFF`） |
| `ECHO_SQL` | `false` | 设为 `true` 打印每条 SQL（调试用） |

## 4. API 一览

所有接口前缀 `/api/v1`，请求/响应均为 JSON。

### 原料 Materials

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/materials` | 创建原料（过敏原自动 trim、小写、去重），初始版本 `revision=1` |
| GET | `/materials` | 原料列表 |
| GET | `/materials/{id}` | 原料详情（含当前版本过敏原集合） |
| GET | `/materials/{id}/revisions` | 全部修订历史（**历史永久保留**） |
| POST | `/materials/{id}/corrections` | 更正过敏原（全量替换集合），需带 `expected_revision` |

更正请求体：

```json
{ "expected_revision": 1, "allergens": ["wheat", "gluten"], "note": "供应商更正" }
```

- `expected_revision` 必须等于服务端当前版本，否则 `409 version_conflict`
- 成功后版本号 +1，旧版本在 `/revisions` 中仍可查

### 批次 Batches

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/batches` | 创建批次，可带初始来源 |
| GET | `/batches` | 批次列表 |
| GET | `/batches/{id}` | 批次详情（含来源、定版状态） |
| PUT | `/batches/{id}/sources` | 修改**未定版**批次来源（全量替换） |
| POST | `/batches/{id}/finalize` | 定版（原子冻结声明） |
| GET | `/batches/{id}/declaration` | 过敏原声明（核心查询） |

来源引用：`{"type": "material", "id": "M-FLOUR"}` 或 `{"type": "batch", "id": "B-DOUGH"}`。

错误响应统一为：

```json
{ "error": { "code": "unknown_source", "message": "未知原料来源: GHOST" } }
```

| HTTP | code | 触发场景 |
| --- | --- | --- |
| 400 | `empty_sources` | 来源为空（创建后定版、或把来源改为空） |
| 400 | `unknown_source` | 引用了不存在的原料/批次 |
| 400 | `cyclic_reference` | 来源图存在环（含自引用） |
| 404 | `not_found` | 原料/批次不存在 |
| 409 | `version_conflict` | 更正携带的期望版本过期 |
| 409 | `already_finalized` | 已定版批次改来源/重复定版 |
| 409 | `id_conflict` | 自定义 ID 冲突 |

### 声明查询返回示例（已定版且上游发生更正）

```json
{
  "batch_id": "B-CAKE",
  "finalized": true,
  "frozen": {
    "allergens": [
      { "allergen": "egg",  "paths": [["B-CAKE", "M-EGG"]] },
      { "allergen": "milk", "paths": [["B-CAKE", "B-DOUGH", "M-MILK"]] },
      { "allergen": "wheat","paths": [["B-CAKE", "B-DOUGH", "M-FLOUR"]] }
    ],
    "material_versions": { "M-EGG": 1, "M-FLOUR": 1, "M-MILK": 1 }
  },
  "current": {
    "allergens": [
      { "allergen": "egg",   "paths": [["B-CAKE", "M-EGG"]] },
      { "allergen": "gluten","paths": [["B-CAKE", "B-DOUGH", "M-FLOUR"]] },
      { "allergen": "milk",  "paths": [["B-CAKE", "B-DOUGH", "M-MILK"]] },
      { "allergen": "wheat", "paths": [["B-CAKE", "B-DOUGH", "M-FLOUR"]] }
    ],
    "material_versions": { "M-EGG": 1, "M-FLOUR": 2, "M-MILK": 1 }
  },
  "added": ["gluten"],
  "removed": [],
  "needs_review": true
}
```

- `frozen`：定版瞬间原子冻结的声明（过敏原、来源原料版本、每条路径）
- `current`：按当前全部可达原料现算的结果（定版批次也会沿其冻结的来源边继续向上游追溯）
- `added` / `removed`：现算集合相对定版集合**新增/消失**的过敏原；任一非空则 `needs_review=true`

## 5. 调用示例

```bash
# curl 版（建议服务已清空数据后执行）
bash examples/api_examples.sh
bash examples/api_examples.sh http://127.0.0.1:8080

# Python 版（httpx，含完整业务流程与内置断言）
pip install httpx
python examples/demo.py
BASE_URL=http://127.0.0.1:8080 python examples/demo.py
```

Python 示例覆盖：创建原料与批次 → 嵌套来源图的并集与路径 → 定版 → 乐观锁更正（过期版本被拒、历史保留）→ 待复核差异（新增/消失）→ 空来源/未知来源/循环引用/定版保护。

## 6. 数据模型与并发语义

```
materials 1──* material_revisions 1──* material_revision_allergens
batches   1──* batch_sources (-> material | batch)
batches   1──* frozen_declarations (定版原子快照：原料版本 + 过敏原 + 路径 JSON)
```

关键保证：

1. **原子定版**：遍历现算结果与写入 `frozen_declarations`、置 `finalized=1` 在同一个 `BEGIN IMMEDIATE` 事务内提交，外部只能看到“完全没定版”或“完整快照已定版”两种状态。
2. **更正/定版串行化**：所有写事务开始即取 SQLite RESERVED 写锁。并发原料更正经版本号乐观检查，恰有一个成功；与定版并发时，定版看到的原料版本集合必然对应某个完整提交，不会出现“版本号是新的、过敏原行是旧的”这类拼接状态。
3. **快照即证据**：冻结构逐行保存 `(原料, 版本, 过敏原, 来源路径)`，即使未来原料继续更正，定版声明仍可逐路径复核。
4. **路径去重**：图遍历以集合收集路径，同一原料经不同路径到达会分别列出，重复引用同一来源只计一次。

## 7. 测试

```bash
pip install -r requirements.txt pytest
python -m pytest tests/ -q
```

测试包含 11 个用例：并集/路径/菱形图/去重、空/未知/循环来源、乐观锁与历史、定版不可变、上游更正差异与待复核，以及两个多线程并发用例（并发更正串行化、更正与定版并发时快照与某一完整修订版本严格一致）。

## 8. 目录结构

```
.
├── app/
│   ├── main.py          # FastAPI 应用（lifespan 内初始化）
│   ├── routers.py       # HTTP 路由
│   ├── schemas.py       # 请求/响应模型、过敏原规范化
│   ├── service.py       # 业务事务（更正乐观锁、定版快照、差异计算）
│   ├── graph.py         # 来源图遍历：并集、路径、循环检测
│   ├── database.py      # SQLite 连接与 IMMEDIATE 事务
│   ├── errors.py        # 领域错误 -> HTTP 状态码
│   ├── config.py        # 环境变量配置
│   ├── init_db.py       # python -m app.init_db
│   └── schema.sql       # 表结构
├── scripts/entrypoint.sh
├── examples/demo.py, examples/api_examples.sh
├── tests/test_api.py
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```
