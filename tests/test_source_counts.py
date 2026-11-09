"""来源管理节点计数口径验证"""
import os
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

tmpdir = tempfile.mkdtemp(prefix="srccnt_test_")
from src import config as cfg_mod
cfg_mod._config = {
    "app": {"name": "Sanl", "version": "test", "port": 8899},
    "database": {"path": os.path.join(tmpdir, "t.db").replace("\\", "/")},
    "gfw": {}, "scheduler": {}, "subs_check": {}, "sources": {},
}
from src.schema import repository
repository.init_db()

# 场景：源 A 解析 10 个节点，其中 3 个与源 B 重复（指纹相同，归属被 B 抢走）
a = repository.add_source("源A", "https://a.example/sub", "github")
b = repository.add_source("源B", "https://b.example/sub", "github")

def nd(i):
    return {"server": f"10.0.0.{i}", "port": 443, "password": "pw", "cipher": "aes-128-gcm"}

# A 导入 10 个
repository.upsert_nodes_bulk([{
    "subscribe_url": a.url, "source_id": a.id, "node_name": f"A-{i}",
    "node_type": "ss", "node_data": nd(i)} for i in range(10)])
# B 导入其中 3 个相同指纹（10,11,12 → 0,1,2 冲突）+ 5 个自己的
repository.upsert_nodes_bulk(
    [{"subscribe_url": b.url, "source_id": b.id, "node_name": f"B-dup-{i}",
      "node_type": "ss", "node_data": nd(i)} for i in range(3)] +
    [{"subscribe_url": b.url, "source_id": b.id, "node_name": f"B-{i}",
      "node_type": "ss", "node_data": nd(100 + i)} for i in range(5)])
# 模拟"上次解析数"与实际不符
repository.update_source_status(a.id, 1, 10)   # A 声称解析 10 个
repository.update_source_status(b.id, 1, 8)    # B 声称解析 8 个

# ---------- 1. 实际归属 vs 声称解析数 ----------
print("=== 1. 实际归属 vs 上次解析数 ===")
counts = repository.count_nodes_by_source()
with repository.get_connection() as conn:
    a_actual = conn.execute("SELECT COUNT(*) FROM nodes WHERE source_id=?", (a.id,)).fetchone()[0]
    b_actual = conn.execute("SELECT COUNT(*) FROM nodes WHERE source_id=?", (b.id,)).fetchone()[0]
print(f"  A: 声称 10，实际 {a_actual}；B: 声称 8，实际 {b_actual}")
# 指纹去重后 A 10 个、B 5 个（B 的 3 个 dup 因后导入，指纹冲突时 ON CONFLICT
# 会把 source_id 改成 B → 实际分布取决于导入顺序；关键是两数之和 == 总数）
total = repository.count_nodes()
assert a_actual + b_actual == total, (a_actual, b_actual, total)
print(f"  ✓ 实际归属之和 {a_actual + b_actual} == 总节点 {total}")

# ---------- 2. count_nodes_by_source 返回值与库一致 ----------
print("\n=== 2. count_nodes_by_source 口径 ===")
assert counts.get(a.id) == a_actual, counts
assert counts.get(b.id) == b_actual, counts
print(f"  ✓ {counts}")

# ---------- 3. 健康接口带 actual_nodes ----------
print("\n=== 3. /sources/health 带 actual_nodes ===")
from src.api.api import get_sources_health
from src.api.cache import invalidate_all
import asyncio, json

async def _health():
    invalidate_all()
    resp = await get_sources_health()
    if hasattr(resp, "body"):
        return json.loads(resp.body)
    return resp

health = asyncio.run(_health())
hmap = {h["id"]: h for h in health}
assert hmap[a.id]["actual_nodes"] == a_actual, hmap[a.id]
assert hmap[b.id]["actual_nodes"] == b_actual, hmap[b.id]
assert hmap[a.id]["node_count"] == 10   # 上次解析数保留原语义
print(f"  ✓ actual_nodes(A)={hmap[a.id]['actual_nodes']} node_count(A)={hmap[a.id]['node_count']}")

# ---------- 4. 孤立节点计数 ----------
print("\n=== 4. 孤立节点 ===")
# 加一个无源节点（测速回填场景）
repository.apply_check_results([{
    "node_type": "ss", "node_data": {"server": "99.99.99.99", "port": 443,
                                     "password": "x", "cipher": "aes-128-gcm"},
    "node_name": "orphan", "latency": 100,
}])
orphan = repository.count_orphan_nodes()
print("  孤立节点:", orphan)
assert orphan == 1, orphan
# 卡片逻辑：actualSum + orphan == pool_total
pool_total = repository.count_nodes()
assert a_actual + b_actual + orphan == pool_total
print(f"  ✓ 源归属 {a_actual + b_actual} + 孤立 {orphan} == 全池 {pool_total}")

print("\n=== 全部断言通过 ===")
