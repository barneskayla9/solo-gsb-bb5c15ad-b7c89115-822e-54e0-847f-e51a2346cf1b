#!/usr/bin/env bash
# 批次过敏原声明追溯服务 —— curl 调用示例
# 用法: bash examples/api_examples.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8080}"
API="$BASE/api/v1"
ct='Content-Type: application/json'

echo "## 0. 健康检查"
curl -s "$API/health"; echo

echo "## 1. 创建原料（过敏原自动小写去重；返回当前版本 current_revision=1）"
curl -s -X POST "$API/materials" -H "$ct" \
  -d '{"id":"M-FLOUR","name":"面粉","allergens":["Wheat","wheat"]}'; echo
curl -s -X POST "$API/materials" -H "$ct" \
  -d '{"id":"M-MILK","name":"奶粉","allergens":["Milk"]}'; echo

echo "## 2. 创建批次（引用原料）"
curl -s -X POST "$API/batches" -H "$ct" -d '{
  "id":"B-DOUGH","name":"面团",
  "sources":[{"type":"material","id":"M-FLOUR"},{"type":"material","id":"M-MILK"}]
}'; echo

echo "## 3. 下游批次引用其他批次"
curl -s -X POST "$API/batches" -H "$ct" -d '{
  "id":"B-CAKE","name":"蛋糕",
  "sources":[{"type":"batch","id":"B-DOUGH"}]
}'; echo

echo "## 4. 未定版查询：按全部可达原料当前版本取并集，给出每项来源路径（去重）"
curl -s "$API/batches/B-CAKE/declaration" | python3 -m json.tool

echo "## 5. 定版（原子冻结声明集合、原料版本、路径）"
curl -s -X POST "$API/batches/B-DOUGH/finalize"; echo
curl -s -X POST "$API/batches/B-CAKE/finalize"; echo

echo "## 6. 原料更正：期望版本必须为当前版本，否则 409 version_conflict"
curl -s -X POST "$API/materials/M-FLOUR/corrections" -H "$ct" \
  -d '{"expected_revision":1,"allergens":["wheat","gluten"]}'; echo
echo "   -- 历史版本保留："
curl -s "$API/materials/M-FLOUR/revisions" | python3 -m json.tool

echo "## 7. 上游更正后查询已定版批次：frozen + current + added/removed + needs_review"
curl -s "$API/batches/B-CAKE/declaration" | python3 -m json.tool

echo "## 8. 错误示例"
echo "   -- 旧版本更正被拒绝:"
curl -s -X POST "$API/materials/M-FLOUR/corrections" -H "$ct" \
  -d '{"expected_revision":1,"allergens":["wheat"]}'; echo
echo "   -- 已定版批次改来源被拒绝 (409):"
curl -s -X PUT "$API/batches/B-DOUGH/sources" -H "$ct" \
  -d '{"sources":[{"type":"material","id":"M-MILK"}]}'; echo
echo "   -- 空来源 (400):"
curl -s -X POST "$API/batches" -H "$ct" -d '{"id":"B-X","name":"x","sources":[]}'; echo
curl -s -X PUT "$API/batches/B-X/sources" -H "$ct" -d '{"sources":[]}'; echo
echo "   -- 未知来源 (400):"
curl -s -X POST "$API/batches" -H "$ct" \
  -d '{"id":"B-Y","name":"y","sources":[{"type":"material","id":"GHOST"}]}'; echo
