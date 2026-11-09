"""三项新功能隔离验证：
1. 源删除（硬删 + URL 墓碑防复活）
2. 节点重命名（自定义名不被抓取/测速回填覆盖）
3. 国家/地区筛选与 IP 字段
"""
import json
import os
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

tmpdir = tempfile.mkdtemp(prefix="sanl_feat_test_")
db_path = os.path.join(tmpdir, "t.db").replace("\\", "/")

from src import config as cfg_mod
cfg_mod._config = {
    "app": {"name": "Sanl", "version": "test", "port": 8899},
    "database": {"path": db_path},
    "gfw": {}, "scheduler": {}, "subs_check": {}, "sources": {},
}
from src.schema import repository
repository.init_db()


def node_data(server, port):
    return {"server": server, "port": port, "password": "pw"}


# ================= 1. 源删除 =================
print("=== 1. 源删除（硬删 + 墓碑）===")
s = repository.add_source("测试源", "https://example.com/sub.txt", "github")
assert s is not None, "add_source 失败"
sid = s.id
srcs = [x for x in repository.list_sources(enabled_only=False) if x.id == sid]
assert len(srcs) == 1
print("✓ 源已创建 id =", sid)

# 删源
res = repository.delete_sources([sid])
assert res["deleted"] == 1, res
assert res["tombstoned"] == 1, res
print("✓ 硬删成功:", res)

# 源真的没了
assert not [x for x in repository.list_sources(enabled_only=False) if x.id == sid], "源仍在列表里"
print("✓ 刷新后源不再出现")

# 墓碑存在
assert repository.count_source_tombstones() == 1
tomb = repository.list_source_tombstones()
assert tomb[0]["url"] == "https://example.com/sub.txt", tomb
print("✓ URL 墓碑已落库:", tomb[0]["name"])

# 复活路径 ①：重新 add_source（预置源再导入）→ 必须被拦截
again = repository.add_source("测试源", "https://example.com/sub.txt", "github")
assert again is None, "源被复活了！"
assert not [x for x in repository.list_sources(enabled_only=False) if x.url == "https://example.com/sub.txt"]
print("✓ 复活路径①（重新导入）已拦截")

# 复活路径 ②：reenable_expired_sources（24h 自动重启用）→ 必须被拦截
with repository.get_connection() as conn:
    conn.execute("INSERT INTO sources (name,url,source_type,enabled,fail_count,updated_at) "
                 "VALUES (?,?,?,0,5,0)", ("僵尸源", "https://example.com/sub.txt", "github"))
    conn.commit()
n = repository.reenable_expired_sources()
assert n == 0, f"僵尸源被自动复活了（{n}）"
print("✓ 复活路径②（24h 自动重启用）已拦截")

# 复活路径 ③：record_source_success（抓取成功强制 enabled=1）→ 必须被拦截
with repository.get_connection() as conn:
    row = conn.execute("SELECT id FROM sources WHERE url=?", ("https://example.com/sub.txt",)).fetchone()
    zid = row["id"]
repository.record_source_success(zid)
with repository.get_connection() as conn:
    row = conn.execute("SELECT enabled FROM sources WHERE id=?", (zid,)).fetchone()
assert row["enabled"] == 0, "僵尸源被抓取成功复活了！"
print("✓ 复活路径③（抓取成功）已拦截")

# 恢复墓碑后可以重新导入（先清掉测试中手工插入的僵尸行，它占了 URL 唯一位）
with repository.get_connection() as conn:
    conn.execute("DELETE FROM sources WHERE url=?", ("https://example.com/sub.txt",))
    conn.commit()
repository.restore_source_tombstones(["https://example.com/sub.txt"])
back = repository.add_source("测试源", "https://example.com/sub.txt", "github")
assert back is not None, "恢复后仍无法导入"
print("✓ 恢复墓碑后可重新导入")

# ================= 2. 节点重命名 =================
print("\n=== 2. 节点重命名 ===")
fp = repository.node_fingerprint("ss", node_data("1.2.3.4", 443))
repository.upsert_nodes_bulk([{
    "subscribe_url": "https://example.com/sub.txt", "source_id": None,
    "node_name": "原名", "node_type": "ss", "node_data": node_data("1.2.3.4", 443),
}])
nid = repository.get_node_by_fingerprint(fp).id if hasattr(repository, "get_node_by_fingerprint") else None
with repository.get_connection() as conn:
    nid = conn.execute("SELECT id FROM nodes WHERE fingerprint=?", (fp,)).fetchone()["id"]
print("✓ 节点已入库 id =", nid)

ok = repository.rename_node(nid, "我的香港节点")
assert ok is True
with repository.get_connection() as conn:
    row = conn.execute("SELECT node_name, node_data FROM nodes WHERE id=?", (nid,)).fetchone()
assert row["node_name"] == "我的香港节点", row
assert json.loads(row["node_data"]).get("custom_name") == "我的香港节点"
print("✓ 重命名成功，custom_name 已写入 node_data")

# 抓取回填不得覆盖自定义名
repository.upsert_nodes_bulk([{
    "subscribe_url": "https://example.com/sub.txt", "source_id": None,
    "node_name": "上游改的名字", "node_type": "ss", "node_data": node_data("1.2.3.4", 443),
}])
with repository.get_connection() as conn:
    row = conn.execute("SELECT node_name FROM nodes WHERE id=?", (nid,)).fetchone()
assert row["node_name"] == "我的香港节点", f"自定义名被覆盖: {row['node_name']}"
print("✓ 抓取回填未覆盖自定义名")

# 测速回填也不得覆盖
repository.apply_check_results([{
    "node_type": "ss", "node_data": node_data("1.2.3.4", 443),
    "node_name": "测速返回的名字", "download_speed": 1024, "latency": 100,
}])
with repository.get_connection() as conn:
    row = conn.execute("SELECT node_name FROM nodes WHERE id=?", (nid,)).fetchone()
assert row["node_name"] == "我的香港节点", f"自定义名被测速覆盖: {row['node_name']}"
print("✓ 测速回填未覆盖自定义名")

# 空名拒绝
assert repository.rename_node(nid, "   ") is False
assert repository.rename_node(999999, "x") is False
print("✓ 空名 / 不存在的节点均被拒绝")

# ================= 3. 国家/地区 + IP =================
print("\n=== 3. 国家/地区筛选与 IP 字段 ===")
repository.update_node_geo(nid, "🇭🇰", "HK", "香港")
countries = repository.list_countries()
assert any(c["country_code"] == "HK" for c in countries), countries
print("✓ 国家列表:", countries)

hk = repository.list_nodes(country_code="HK")
assert len(hk) == 1, hk
other = repository.list_nodes(country_code="JP")
assert len(other) == 0
print("✓ country_code 筛选生效")

# ================= 4. 源删除连带节点清理 =================
print("\n=== 4. 源删除连带节点清理 ===")
s2 = repository.add_source("带节点的源", "https://example.com/n2.txt", "github")
repository.upsert_nodes_bulk([{
    "subscribe_url": s2.url, "source_id": s2.id,
    "node_name": "子节点", "node_type": "ss", "node_data": node_data("9.9.9.9", 443),
}])
with repository.get_connection() as conn:
    cnt = conn.execute("SELECT COUNT(*) FROM nodes WHERE source_id=?", (s2.id,)).fetchone()[0]
assert cnt == 1, cnt
res = repository.delete_sources([s2.id])
assert res["deleted"] == 1, res
assert res["nodes_deleted"] == 1, res
with repository.get_connection() as conn:
    cnt = conn.execute("SELECT COUNT(*) FROM nodes WHERE source_id=?", (s2.id,)).fetchone()[0]
    # 子节点也要落节点墓碑（防下轮抓取复活）
    tn = conn.execute("SELECT COUNT(*) FROM deleted_node_tombstones").fetchone()[0]
assert cnt == 0, f"节点残留 {cnt}"
assert tn >= 1, tn
print(f"✓ 源与 {res['nodes_deleted']} 个节点一并删除，节点墓碑 {tn} 条")

print("\n=== 全部断言通过 ===")
