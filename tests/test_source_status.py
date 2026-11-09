"""源抓取失败状态落库 + 健康状态判定验证"""
import os
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

tmpdir = tempfile.mkdtemp(prefix="srcst_test_")
from src import config as cfg_mod
cfg_mod._config = {
    "app": {"name": "Sanl", "version": "test", "port": 8899},
    "database": {"path": os.path.join(tmpdir, "t.db").replace("\\", "/")},
    "gfw": {}, "scheduler": {}, "subs_check": {}, "sources": {},
}
from src.schema import repository
repository.init_db()

# ---------- 1. 失败后 last_status 必须变 2 ----------
print("=== 1. 失败状态落库 ===")
s = repository.add_source("失败源", "https://fail.example/sub", "github")
assert s and s.id
# 初始 last_status=0（待抓取）
with repository.get_connection() as conn:
    st0 = conn.execute("SELECT last_status, fail_count FROM sources WHERE id=?", (s.id,)).fetchone()
print("  初始:", dict(st0))
assert st0["last_status"] == 0

repository.record_source_failure(s.id)
with repository.get_connection() as conn:
    st1 = conn.execute("SELECT last_status, fail_count FROM sources WHERE id=?", (s.id,)).fetchone()
print("  失败 1 次后:", dict(st1))
assert st1["last_status"] == 2, "失败后 last_status 仍为 0 → 前端会显示成待抓取！"
assert st1["fail_count"] == 1
print("  ✓ 失败后 last_status=2（不再显示待抓取）")

# ---------- 2. 连续失败达到阈值自动禁用 ----------
print("\n=== 2. 阈值自动禁用 ===")
for _ in range(4):
    repository.record_source_failure(s.id)
with repository.get_connection() as conn:
    st2 = conn.execute("SELECT last_status, fail_count, enabled FROM sources WHERE id=?", (s.id,)).fetchone()
print("  失败 5 次后:", dict(st2))
assert st2["fail_count"] == 5
assert st2["enabled"] == 0, "达阈值未自动禁用"
assert st2["last_status"] == 2
print("  ✓ 达阈值自动禁用且 last_status=2")

# ---------- 3. 成功后恢复 last_status=1 ----------
print("\n=== 3. 成功状态恢复 ===")
repository.enable_source(s.id, 1)
repository.update_source_status(s.id, 1, 10)
repository.record_source_success(s.id)
with repository.get_connection() as conn:
    st3 = conn.execute("SELECT last_status, fail_count, enabled, node_count FROM sources WHERE id=?", (s.id,)).fetchone()
print("  成功后:", dict(st3))
assert st3["last_status"] == 1
assert st3["fail_count"] == 0
assert st3["enabled"] == 1
assert st3["node_count"] == 10
print("  ✓ 成功后 last_status=1 且计数清零")

# ---------- 4. 健康状态判定 ----------
print("\n=== 4. 健康状态判定 ===")
s2 = repository.add_source("新源", "https://new.example/sub", "github")
s3 = repository.add_source("异常源", "https://bad.example/sub", "github")
# s2 保持 last_status=0 → pending
# s3: 禁用且非失败态 → unhealthy
repository.disable_sources([s3.id])

from src.api.api import get_sources_health
from src.api.cache import invalidate_all
import asyncio, json

async def _health_map():
    invalidate_all()   # 清 5s TTL 缓存，读到最新库态
    resp = await get_sources_health()
    # @cached 装饰器返回 JSONResponse；解出 body
    if hasattr(resp, "body"):
        return {h["id"]: h["state"] for h in json.loads(resp.body)}
    return {h["id"]: h["state"] for h in resp}

hmap = asyncio.run(_health_map())
print("  状态表:", hmap)
assert hmap[s.id] == "healthy", hmap
assert hmap[s2.id] == "pending", hmap
assert hmap[s3.id] == "unhealthy", hmap
print("  ✓ healthy/pending/unhealthy 判定正确")

# failed 态：再造一个 last_status=2 且未达禁用阈值的
s4 = repository.add_source("失败中源", "https://f2.example/sub", "github")
repository.record_source_failure(s4.id)   # fail_count=1, last_status=2
hmap = asyncio.run(_health_map())
assert hmap[s4.id] == "failed", hmap
print("  ✓ failed 态独立判定（不再冒充待抓取）")

print("\n=== 全部断言通过 ===")
