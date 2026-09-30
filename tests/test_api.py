"""端到端 API 测试：使用 FastAPI TestClient + 临时 SQLite 文件。"""
from __future__ import annotations

import threading
import uuid
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app import config as config_module
from app.database import init_db


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    test_settings = replace(config_module.settings, db_path=str(db_file))
    # database.get_conn 在调用时读取 app.database.settings，必须替换该模块绑定
    import app.database as db_module
    monkeypatch.setattr(db_module, "settings", test_settings)
    init_db(str(db_file))
    from app.main import app
    with TestClient(app) as c:
        yield c


def make_material(client, allergens, mid=None):
    mid = mid or f"M-{uuid.uuid4().hex[:8]}"
    r = client.post("/api/v1/materials", json={"id": mid, "name": mid, "allergens": allergens})
    assert r.status_code == 201, r.text
    return r.json()


def make_batch(client, sources, bid=None, name="b"):
    bid = bid or f"B-{uuid.uuid4().hex[:8]}"
    r = client.post("/api/v1/batches", json={"id": bid, "name": name, "sources": sources})
    assert r.status_code == 201, r.text
    return r.json()


# ---------------- 基础并集与路径 ----------------


def test_allergen_union_and_paths(client):
    make_material(client, ["Wheat"], "M1")
    make_material(client, ["Milk", " Soy "], "M2")  # 规范化：去空白小写
    b = make_batch(client, [{"type": "material", "id": "M1"}, {"type": "material", "id": "M2"}], "B1")
    decl = client.get("/api/v1/batches/B1/declaration").json()
    assert decl["finalized"] is False
    assert decl["frozen"] is None
    allergens = {e["allergen"] for e in decl["current"]["allergens"]}
    assert allergens == {"wheat", "milk", "soy"}
    milk = next(e for e in decl["current"]["allergens"] if e["allergen"] == "milk")
    assert milk["paths"] == [["B1", "M2"]]
    assert decl["current"]["material_versions"] == {"M1": 1, "M2": 1}


def test_nested_batch_paths_and_dedup(client):
    # 同一原料经两条路径到达，过敏原路径都保留；同一批次被引用两次 → 路径重复只计一次
    make_material(client, ["egg"], "ME")
    make_batch(client, [{"type": "material", "id": "ME"}], "BA")
    make_batch(client, [{"type": "batch", "id": "BA"}, {"type": "batch", "id": "BA"}], "BB")
    decl = client.get("/api/v1/batches/BB/declaration").json()
    egg = next(e for e in decl["current"]["allergens"] if e["allergen"] == "egg")
    assert egg["paths"] == [["BB", "BA", "ME"]]  # 重复来源只计一次


def test_diamond_graph_path_enumeration(client):
    # BR -> BL,BR ; BL,BR -> M ; 应枚举到两条不同路径
    make_material(client, ["nut"], "MN")
    make_batch(client, [{"type": "material", "id": "MN"}], "BL")
    make_batch(client, [{"type": "material", "id": "MN"}], "BR")
    make_batch(client, [{"type": "batch", "id": "BL"}, {"type": "batch", "id": "BR"}], "BT")
    decl = client.get("/api/v1/batches/BT/declaration").json()
    nut = decl["current"]["allergens"][0]
    assert nut["paths"] == [["BT", "BL", "MN"], ["BT", "BR", "MN"]]


# ---------------- 来源校验错误 ----------------


def test_empty_unknown_cyclic(client):
    make_material(client, ["x"], "M1")

    # 创建空来源批次允许（未定版）
    r = client.post("/api/v1/batches", json={"id": "E1", "name": "e", "sources": []})
    assert r.status_code == 201

    # 定版空来源 → 400
    r = client.post("/api/v1/batches/E1/finalize")
    assert r.status_code == 400 and r.json()["error"]["code"] == "empty_sources"

    # 改成空来源 → 400
    r = client.put("/api/v1/batches/E1/sources", json={"sources": []})
    assert r.status_code == 400 and r.json()["error"]["code"] == "empty_sources"

    # 未知原料/批次
    r = client.post("/api/v1/batches", json={"id": "U1", "name": "u", "sources": [{"type": "material", "id": "GHOST"}]})
    assert r.status_code == 400 and r.json()["error"]["code"] == "unknown_source"
    r = client.post("/api/v1/batches", json={"id": "U2", "name": "u",
                                      "sources": [{"type": "batch", "id": "GHOST-B"}]})
    assert r.status_code == 400 and r.json()["error"]["code"] == "unknown_source"

    # 自引用 → 循环
    r = client.post("/api/v1/batches", json={"id": "S1", "name": "s", "sources": [{"type": "batch", "id": "S1"}]})
    assert r.status_code == 400 and r.json()["error"]["code"] == "cyclic_reference"

    # 间接环：A->B（合法），随后 B->A（成环）
    make_batch(client, [{"type": "material", "id": "M1"}], "CA")
    make_batch(client, [{"type": "batch", "id": "CA"}], "CB")
    r = client.put("/api/v1/batches/CA/sources", json={"sources": [{"type": "batch", "id": "CB"}]})
    assert r.status_code == 400 and r.json()["error"]["code"] == "cyclic_reference"

    # 重复来源 → 静默去重，只保留一条（“重复来源只计一次”）
    r = client.post("/api/v1/batches", json={"id": "D1", "name": "d", "sources": [
        {"type": "material", "id": "M1"}, {"type": "material", "id": "M1"}]})
    assert r.status_code == 201
    assert r.json()["sources"] == [{"type": "material", "id": "M1"}]


def test_not_found(client):
    assert client.get("/api/v1/batches/NOPE/declaration").status_code == 404
    assert client.post("/api/v1/materials/NOPE/corrections",
                       json={"expected_revision": 1, "allergens": []}).status_code == 404


# ---------------- 定版与更正 ----------------


def test_finalize_freezes_and_immutable(client):
    make_material(client, ["wheat"], "M1")
    make_batch(client, [{"type": "material", "id": "M1"}], "B1")
    assert client.post("/api/v1/batches/B1/finalize").status_code == 200

    # 来源不可改 / 不可重复定版
    r = client.put("/api/v1/batches/B1/sources", json={"sources": [{"type": "material", "id": "M1"}]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "already_finalized"
    assert client.post("/api/v1/batches/B1/finalize").status_code == 409

    decl = client.get("/api/v1/batches/B1/declaration").json()
    assert decl["frozen"]["allergens"][0]["allergen"] == "wheat"
    assert decl["frozen"]["material_versions"] == {"M1": 1}
    assert decl["needs_review"] is False


def test_correction_optimistic_lock_and_history(client):
    make_material(client, ["wheat"], "M1")

    # 旧版本拒绝
    r = client.post("/api/v1/materials/M1/corrections",
                    json={"expected_revision": 1, "allergens": ["wheat", "gluten"]})
    # 上面是 v1→v2，成功
    assert r.status_code == 200 and r.json()["current_revision"] == 2

    # 再用旧版本 1 提交 → 409
    r = client.post("/api/v1/materials/M1/corrections",
                    json={"expected_revision": 1, "allergens": ["x"]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "version_conflict"

    # 历史保留：两个版本都在
    revs = client.get("/api/v1/materials/M1/revisions").json()
    assert [x["revision"] for x in revs] == [1, 2]
    assert revs[0]["allergens"] == ["wheat"]
    assert revs[1]["allergens"] == ["gluten", "wheat"]


def test_upstream_correction_diff_and_review(client):
    make_material(client, ["wheat"], "MF")  # 面粉
    make_material(client, ["milk"], "MM")   # 奶粉
    make_batch(client, [{"type": "material", "id": "MF"}, {"type": "material", "id": "MM"}], "BD")
    make_batch(client, [{"type": "batch", "id": "BD"}], "BC")
    client.post("/api/v1/batches/BD/finalize")
    client.post("/api/v1/batches/BC/finalize")

    # 新增 gluten
    r = client.post("/api/v1/materials/MF/corrections",
                    json={"expected_revision": 1, "allergens": ["wheat", "gluten"]})
    assert r.status_code == 200

    # 下游批次 BD 与 BC 都按当前证据重算
    for bid, frozen_rev in (("BD", {"MF": 1, "MM": 1}), ("BC", {"MF": 1, "MM": 1})):
        decl = client.get(f"/api/v1/batches/{bid}/declaration").json()
        assert decl["added"] == ["gluten"], decl
        assert decl["removed"] == []
        assert decl["needs_review"] is True
        assert decl["frozen"]["material_versions"] == frozen_rev
        assert decl["current"]["material_versions"]["MF"] == 2

    # milk 消失
    client.post("/api/v1/materials/MM/corrections", json={"expected_revision": 1, "allergens": []})
    decl = client.get("/api/v1/batches/BC/declaration").json()
    assert "gluten" in decl["added"]
    assert decl["removed"] == ["milk"]

    # 改回与定版一致（仅 wheat）
    r = client.post("/api/v1/materials/MF/corrections",
                    json={"expected_revision": 2, "allergens": ["wheat"]})
    assert r.status_code == 200
    decl = client.get("/api/v1/batches/BC/declaration").json()
    assert decl["needs_review"] is True  # milk 仍缺失
    r = client.post("/api/v1/materials/MM/corrections",
                    json={"expected_revision": 2, "allergens": ["milk"]})
    assert r.status_code == 200
    decl = client.get("/api/v1/batches/BC/declaration").json()
    assert decl["added"] == [] and decl["removed"] == []
    assert decl["needs_review"] is False


def test_finalized_upstream_still_traced_by_current_edges(client):
    # 已定版的上游批次在下游现算时，仍沿其当前（冻结的）来源边向上走
    make_material(client, ["egg"], "ME")
    make_batch(client, [{"type": "material", "id": "ME"}], "BUP")
    client.post("/api/v1/batches/BUP/finalize")
    make_batch(client, [{"type": "batch", "id": "BUP"}], "BDOWN")
    client.post("/api/v1/batches/BDOWN/finalize")
    decl = client.get("/api/v1/batches/BDOWN/declaration").json()
    paths = decl["current"]["allergens"][0]["paths"]
    assert paths == [["BDOWN", "BUP", "ME"]]


# ---------------- 并发：更正与定版不得拼接 ----------------


def test_concurrent_correction_and_finalize(client):
    """N 个线程同时对原料做更正 + 对批次定版，任何定版快照必须对应某个完整提交版本。"""
    make_material(client, ["a0"], "MC")
    make_batch(client, [{"type": "material", "id": "MC"}], "BF")

    errors: list[Exception] = []
    barrier = threading.Barrier(4)

    def correct():
        try:
            barrier.wait()
            # 乐观锁：只有一个更正会成功，其余收到 409 后重试读版本再提交
            for attempt in range(20):
                cur = client.get("/api/v1/materials/MC").json()["current_revision"]
                r = client.post("/api/v1/materials/MC/corrections",
                                json={"expected_revision": cur,
                                      "allergens": [f"a{cur}"]})
                if r.status_code == 200:
                    return
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    def finalize():
        try:
            barrier.wait()
            client.post("/api/v1/batches/BF/finalize")
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=correct) for _ in range(3)]
    threads.append(threading.Thread(target=finalize))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors

    # 关键不变量：冻结构中的原料版本必须存在于修订历史，
    # 且该版本过敏原集合与快照完全一致（不允许出现跨提交拼接）。
    decl = client.get("/api/v1/batches/BF/finalize")
    decl = client.get("/api/v1/batches/BF/declaration").json()
    frozen_rev = decl["frozen"]["material_versions"]["MC"]
    frozen_allergens = {e["allergen"] for e in decl["frozen"]["allergens"]}
    hist = {x["revision"]: set(x["allergens"])
            for x in client.get("/api/v1/materials/MC/revisions").json()}
    assert frozen_rev in hist
    assert frozen_allergens == hist[frozen_rev] == {f"a{frozen_rev - 1}"}


def test_concurrent_corrections_serialize(client):
    """两个基于同一版本的并发更正：恰好一个成功，另一个 409；版本号不跳不重。"""
    make_material(client, ["x"], "MS")
    results: list[int] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def correct(allergens):
        barrier.wait()
        r = client.post("/api/v1/materials/MS/corrections",
                        json={"expected_revision": 1, "allergens": allergens})
        with lock:
            results.append(r.status_code)

    t1 = threading.Thread(target=correct, args=(["y"],))
    t2 = threading.Thread(target=correct, args=(["z"],))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(results) == [200, 409]
    mat = client.get("/api/v1/materials/MS").json()
    assert mat["current_revision"] == 2
