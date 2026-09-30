#!/usr/bin/env python3
"""端到端调用示例：演示完整业务流程。

用法（服务已启动后）：
    python examples/demo.py                 # 默认 http://localhost:8080
    BASE_URL=http://127.0.0.1:8080 python examples/demo.py

覆盖场景：
1. 创建原料（带过敏原集合，版本 1）
2. 创建上游/下游批次并定版，查看过敏原并集与来源路径
3. 上游原料更正（期望版本乐观锁：旧版本被拒、新版本成功，历史保留）
4. 重新查询已定版批次：现算集合、新增/消失过敏原、needs_review 待复核
5. 错误处理：空来源、未知来源、循环引用、已定版不可改
"""
from __future__ import annotations

import os
import sys

import httpx

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8080").rstrip("/")
API = f"{BASE_URL}/api/v1"


def banner(title: str) -> None:
    print(f"\n{'=' * 70}\n  {title}\n{'=' * 70}")


def show(label: str, obj) -> None:
    import json

    print(f"\n--- {label} ---")
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def expect_error(label: str, fn, status: int) -> None:
    try:
        resp = fn()
        # 统一在这里触发状态码异常（调用处的 fn 不必自己 raise_for_status）
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        print(f"[符合预期] {label}: HTTP {exc.response.status_code} -> {exc.response.json()}")
        assert exc.response.status_code == status, f"期望 {status}，实际 {exc.response.status_code}"
        return
    raise AssertionError(f"{label} 应当失败但成功了")


def main() -> None:
    client = httpx.Client(base_url=API, timeout=10.0)
    client.headers.update({"Content-Type": "application/json"})

    # 简单健康检查
    r = client.get("/health")
    r.raise_for_status()
    print(f"服务健康检查通过: {r.json()} (base={API})")

    # 1) 创建原料 ----------------------------------------------------------
    banner("1. 创建原料（首次版本 revision=1）")
    flour = client.post(
        "/materials",
        json={"id": "M-FLOUR", "name": "面粉", "allergens": ["Wheat", " wheat "]},
    )
    flour.raise_for_status()
    show("面粉（重复过敏原去重、小写规范化）", flour.json())

    milk = client.post(
        "/materials", json={"id": "M-MILK", "name": "奶粉", "allergens": ["Milk"]}
    )
    milk.raise_for_status()

    egg = client.post(
        "/materials", json={"id": "M-EGG", "name": "蛋液", "allergens": ["Egg"]}
    )
    egg.raise_for_status()

    # 2) 批次来源图：B-DOUGH -> M-FLOUR, M-MILK；B-CAKE -> B-DOUGH, M-EGG
    banner("2. 创建批次并定版（并集 + 来源路径）")
    dough = client.post(
        "/batches",
        json={
            "id": "B-DOUGH",
            "name": "面团",
            "sources": [
                {"type": "material", "id": "M-FLOUR"},
                {"type": "material", "id": "M-MILK"},
            ],
        },
    )
    dough.raise_for_status()
    show("创建面团批次", dough.json())

    cake = client.post(
        "/batches",
        json={
            "id": "B-CAKE",
            "name": "蛋糕成品",
            "sources": [
                {"type": "batch", "id": "B-DOUGH"},
                {"type": "material", "id": "M-EGG"},
            ],
        },
    )
    cake.raise_for_status()

    decl_before = client.get("/batches/B-CAKE/declaration")
    show("B-CAKE 定版前的现算声明（含每条过敏原的来源路径）", decl_before.json())

    client.post("/batches/B-DOUGH/finalize").raise_for_status()
    client.post("/batches/B-CAKE/finalize").raise_for_status()
    frozen_decl = client.get("/batches/B-CAKE/declaration")
    show("B-CAKE 定版后声明（frozen 已原子冻结）", frozen_decl.json())

    # 3) 原料更正：乐观版本控制 --------------------------------------------
    banner("3. 原料更正（expected_revision 乐观锁）")
    # 3a. 期望版本与当前版本不一致 -> 409 拒绝（这里客户端持有过期认知，乱填 999）
    expect_error(
        "基于过期版本 999 更正面粉",
        lambda: client.post(
            "/materials/M-FLOUR/corrections",
            json={"expected_revision": 999, "allergens": ["wheat", "gluten"]},
        ),
        409,
    )
    # 3b. 正确版本 -> 成功，revision 变为 2，历史保留
    corr = client.post(
        "/materials/M-FLOUR/corrections",
        json={"expected_revision": 1, "allergens": ["wheat", "gluten"], "note": "供应商更正：含麸质"},
    )
    corr.raise_for_status()
    show("面粉更正成功（revision=2）", corr.json())

    revs = client.get("/materials/M-FLOUR/revisions")
    show("面粉完整修订历史（版本 1 保留）", revs.json())

    # 4) 上游更正后查询已定版批次 ------------------------------------------
    banner("4. 上游更正后：现算集合 vs 定版集合 -> 待复核")
    after = client.get("/batches/B-CAKE/declaration")
    data = after.json()
    show("B-CAKE 声明（新增 gluten，frozen 不变）", data)
    assert data["added"] == ["gluten"], data
    assert data["removed"] == []
    assert data["needs_review"] is True
    print("\n[断言通过] added=['gluten']，needs_review=True，冻结构保持 revision=1")

    # 过敏原“消失”场景：奶粉更正为不含 Milk
    client.post(
        "/materials/M-MILK/corrections",
        json={"expected_revision": 1, "allergens": []},
    ).raise_for_status()
    after2 = client.get("/batches/B-CAKE/declaration").json()
    show("奶粉去除 Milk 后（milk 进入 removed）", {
        "added": after2["added"], "removed": after2["removed"], "needs_review": after2["needs_review"],
    })
    assert after2["removed"] == ["milk"], after2

    # 5) 各类错误 ----------------------------------------------------------
    banner("5. 错误处理：空来源 / 未知来源 / 循环引用 / 已定版不可改")
    # 未定版批次允许先建空来源，后续再改；但定版/改空均报错
    client.post(
        "/batches", json={"id": "B-EMPTY", "name": "空批次", "sources": []}
    ).raise_for_status()
    expect_error("空来源批次定版 -> 400", lambda: client.post("/batches/B-EMPTY/finalize"), 400)
    expect_error(
        "改成空来源 -> 400",
        lambda: client.put("/batches/B-EMPTY/sources", json={"sources": []}),
        400,
    )
    expect_error(
        "未知原料来源 -> 400",
        lambda: client.put(
            "/batches/B-EMPTY/sources", json={"sources": [{"type": "material", "id": "NOPE"}]}
        ),
        400,
    )
    # 循环引用：B-DOUGH(已定版) <- 新建 B-X -> B-DOUGH 不构成环；
    # 制造环：创建 B-A -> B-DOUGH，然后尝试 B-DOUGH 改来源指向 B-A（但 B-DOUGH 已定版，先演示定版保护）
    expect_error(
        "已定版批次改来源 -> 409",
        lambda: client.put(
            "/batches/B-DOUGH/sources",
            json={"sources": [{"type": "material", "id": "M-FLOUR"}]},
        ),
        409,
    )
    expect_error(
        "已定版批次再次定版 -> 409",
        lambda: client.post("/batches/B-DOUGH/finalize"),
        409,
    )
    # 真正的环：B-A -> B-B，B-B -> B-A
    client.post(
        "/batches", json={"id": "B-A", "name": "环A", "sources": [{"type": "batch", "id": "B-DOUGH"}]}
    ).raise_for_status()
    client.post(
        "/batches", json={"id": "B-B", "name": "环B", "sources": [{"type": "batch", "id": "B-A"}]}
    ).raise_for_status()
    expect_error(
        "设置 B-A -> B-B 形成环 -> 400",
        lambda: client.put(
            "/batches/B-A/sources",
            json={"sources": [{"type": "batch", "id": "B-B"}]},
        ),
        400,
    )

    print("\n全部演示与断言通过 ✔")


if __name__ == "__main__":
    try:
        main()
    except httpx.ConnectError:
        print(f"无法连接 {API}，请先启动服务：docker compose up --build", file=sys.stderr)
        sys.exit(1)
