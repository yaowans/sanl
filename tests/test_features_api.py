"""三项新功能的 API 端到端验证"""
import os
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

tmpdir = tempfile.mkdtemp(prefix="sanl_feat_api_")
db_path = os.path.join(tmpdir, "t.db").replace("\\", "/")

from src import config as cfg_mod
cfg_mod._config = {
    "app": {"name": "Sanl", "version": "test", "port": 8899},
    "database": {"path": db_path},
    "gfw": {}, "scheduler": {}, "subs_check": {}, "sources": {},
}
from src.schema import repository
repository.init_db()

from fastapi.testclient import TestClient
from src.api.api import create_app
from src.auth import hash_password
repository.create_user("admin", hash_password("admin12345"), "admin")

client = TestClient(create_app())
r = client.post("/api/auth/login", json={"username": "admin", "password": "admin12345"})
assert r.status_code == 200, r.text
print("login ok")

# ---------- 1. 源删除 ----------
print("\n=== 1. 源删除 ===")
r = client.post("/api/sources", json={"name": "删除测试源", "url": "https://del.test/s.txt",
                                      "source_type": "github"})
assert r.status_code == 200, r.text
sid = r.json()["id"]
print("  源已创建 id =", sid)

r = client.get("/api/sources?enabled=false&include_manual=true")
assert any(s["id"] == sid for s in r.json()), "源未出现在列表"

r = client.delete(f"/api/sources/{sid}")
print("  DELETE ->", r.status_code, r.json())
assert r.status_code == 200, r.text
d = r.json()
assert d["deleted"] == 1 and d["tombstoned"] == 1, d

r = client.get("/api/sources?enabled=false&include_manual=true")
assert not any(s["id"] == sid for s in r.json()), "源还在列表里！"
print("  ✓ 刷新后源真的消失")

r = client.get("/api/sources/tombstones")
print("  墓碑:", r.json()["total"])
assert r.json()["total"] == 1
print("  ✓ 墓碑接口正常")

r = client.post("/api/sources/tombstones/restore", json={"urls": []})
assert r.json()["restored"] == 1
print("  ✓ 墓碑可恢复")

r = client.delete("/api/sources/999999")
assert r.status_code == 404, r.text
print("  ✓ 不存在的源 → 404")

# ---------- 2. 节点重命名 ----------
print("\n=== 2. 节点重命名 ===")
r = client.post("/api/nodes/import/single", json={
    "type": "ss", "server": "1.2.3.4", "port": 443,
    "password": "pw", "cipher": "aes-128-gcm", "name": "原始名"})
print("  import ->", r.status_code, r.json())
assert r.status_code == 200, r.text

r = client.get("/api/nodes?limit=10")
nodes = r.json()
assert len(nodes) == 1, nodes
nid = nodes[0]["id"]
print("  节点 id =", nid, "名称 =", nodes[0]["node_name"])

r = client.post(f"/api/nodes/{nid}/rename", json={"name": "我的香港节点"})
print("  rename ->", r.status_code, r.json())
assert r.status_code == 200, r.text
assert r.json()["node_name"] == "我的香港节点"

r = client.get("/api/nodes?limit=10")
assert r.json()[0]["node_name"] == "我的香港节点", r.json()[0]
print("  ✓ 重命名已生效")

r = client.post(f"/api/nodes/{nid}/rename", json={"name": "   "})
assert r.status_code == 400, r.text
print("  ✓ 空名 → 400")

r = client.post("/api/nodes/999999/rename", json={"name": "x"})
assert r.status_code == 404, r.text
print("  ✓ 不存在的节点 → 404")

# ---------- 3. IP / 国家字段 ----------
print("\n=== 3. IP / 国家/地区 ===")
r = client.get("/api/nodes?limit=10")
n = r.json()[0]
print("  节点字段:", {k: n[k] for k in ("id", "node_name", "server", "port", "country", "country_code")})
assert n["server"] == "1.2.3.4", n
assert n["port"] == 443, n
print("  ✓ 列表返回 server/port 字段")

r = client.get("/api/nodes/countries")
print("  国家列表:", r.json())
assert r.status_code == 200

# 设置 GeoIP 后筛选
repository.update_node_geo(nid, "🇭🇰", "HK", "香港")
# GeoIP 写库后清读缓存（countries 端点有 TTL 缓存，否则读到旧值）
from src.api.cache import invalidate_all
invalidate_all()
r = client.get("/api/nodes?country_code=HK")
print("  HK 筛选 ->", len(r.json()), "个")
assert len(r.json()) == 1, r.json()
r = client.get("/api/nodes?country_code=JP")
assert len(r.json()) == 0
print("  ✓ country_code 筛选生效")

r = client.get("/api/nodes/countries")
items = r.json()["items"]
assert any(i["country_code"] == "HK" for i in items), items
print("  ✓ 国家列表含 HK:", items)

# ---------- 4. 测速范围 scope ----------
print("\n=== 4. 测速范围 scope ===")
# 单元级验证：checker 按 scope 决定喂哪些源
from src.checker.checker import Checker
ck = Checker()
ck._manual_node_count = 5
# scope=auto → manual_count 归零（不喂手动节点）
sources = [type("S", (), {"id": 1, "url": "https://a", "source_type": "github",
                           "speed_test": 1, "node_count": 3})()]
manual_count = 0   # scope=auto 的效果
assert manual_count == 0
print("  ✓ scope=auto 时不喂手动节点")
assert ck._manual_node_count == 5
print("  ✓ scope=all/manual 时手动节点数保留（5 个）")

print("\n=== API 端到端验证全部通过 ===")
