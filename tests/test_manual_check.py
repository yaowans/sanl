"""手工测速 / 信息刷新的隔离验证（不依赖 subs-check 二进制）"""
import os
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

tmpdir = tempfile.mkdtemp(prefix="manual_test_")
db_path = os.path.join(tmpdir, "t.db").replace("\\", "/")

from src import config as cfg_mod
cfg_mod._config = {
    "app": {"name": "Sanl", "version": "test", "port": 8899},
    "database": {"path": db_path},
    "gfw": {}, "scheduler": {}, "subs_check": {}, "sources": {},
}
from src.schema import repository
repository.init_db()

from src import manual_check as mc

# ---------- 准备数据 ----------
src = repository.get_or_create_manual_source("手动导入")
n1 = repository.add_node("manual://x", src.id, "AU01", "hysteria2",
                         {"server": "vpn-au-001.example.com", "port": 443,
                          "password": "pw"}, status="active")
n2 = repository.add_node("manual://x", src.id, "JP01", "vless",
                         {"server": "1.2.3.4", "port": 443, "uuid": "u-1"}, status="active")
n3 = repository.add_node("manual://x", src.id, "自动节点", "ss",
                         {"server": "5.6.7.8", "port": 443, "password": "p",
                          "cipher": "aes-128-gcm"}, status="active")
# n3 属于手动源但语义上当作"另一个源"的节点用于对比
print("准备节点:", n1.id, n2.id, n3.id)

# ---------- 1. 链接生成 ----------
print("\n=== 1. 协议链接生成 ===")
links = mc.build_links([n1, n2, n3])
print("  生成链接:", links)
assert len(links) == 3, links
assert any(l.startswith("hysteria2://") for l in links), links
assert any(l.startswith("vless://") for l in links), links
assert any(l.startswith("ss://") for l in links), links
print("  ✓ 三种协议都生成了链接")

# ---------- 2. 作用域指纹 ----------
print("\n=== 2. 作用域指纹 ===")
scope = mc.scope_fingerprints([n1, n2])
print("  作用域:", scope)
assert len(scope) == 2
assert repository.node_fingerprint(n1.node_type, n1.node_data) in scope
assert repository.node_fingerprint(n3.node_type, n3.node_data) not in scope
print("  ✓ 作用域只含选中的节点")

# ---------- 3. 作用域限定的降级（关键：不能误伤作用域外节点）----------
print("\n=== 3. 作用域限定的状态降级 ===")
# 模拟：n1 测活，n2 没测到 → n2 应降级；n3 不在作用域 → 保持不变
fps = {repository.node_fingerprint(n1.node_type, n1.node_data)}
repository.mark_missing_inactive(fps, scope_fingerprints=scope)
with repository.get_connection() as conn:
    st = {r["id"]: r["status"] for r in conn.execute("SELECT id,status FROM nodes")}
print("  状态:", st)
assert st[n1.id] == "active", st   # 测活的保持 active
assert st[n2.id] == "inactive", st  # 作用域内没测到 → 降级
assert st[n3.id] == "active", st   # 作用域外 → 不受影响！
print("  ✓ 作用域外节点未被误降级（关键正确性）")

# ---------- 4. 全量降级仍能正常工作（回归）----------
print("\n=== 4. 全量降级回归 ===")
# 全量降级默认**不处理手动节点**（手动节点永不被自动降级）→ 用自动源节点验证
auto_src = repository.add_source("自动源", "https://auto.example/sub.txt", "github")
n4 = repository.add_node("https://auto.example/sub.txt", auto_src.id, "自动节点2", "ss",
                         {"server": "9.9.9.9", "port": 443, "password": "p2",
                          "cipher": "aes-128-gcm"}, status="active")
repository.mark_missing_inactive(fps)
with repository.get_connection() as conn:
    st2 = {r["id"]: r["status"] for r in conn.execute("SELECT id,status FROM nodes")}
print("  状态:", st2)
assert st2[n4.id] == "inactive", st2    # 自动源节点 → 降级
assert st2[n3.id] == "active", st2      # 手动源节点 → 保持（原有语义）
print("  ✓ 全量降级：自动源降级、手动源不动（原有行为保持）")

# ---------- 5. 地区补齐 + 规范化命名 ----------
print("\n=== 5. 地区补齐与规范化命名 ===")
# 手工设置地区（模拟 GeoIP 查询结果）
repository.update_node_geo(n1.id, "🇦🇺", "AU", "悉尼")
repository.update_node_geo(n2.id, "🇯🇵", "JP", "东京")
nodes = repository.get_nodes_by_ids([n1.id, n2.id])
renamed = mc.normalize_manual_names(nodes)
print("  重命名数:", renamed)
with repository.get_connection() as conn:
    names = {r["id"]: r["node_name"] for r in conn.execute("SELECT id,node_name FROM nodes")}
print("  名称:", names)
# 新格式：{旗}{CC}_{i}|{IP}
assert names[n1.id] == "🇦🇺AU_1|vpn-au-001.example.com", names
assert names[n2.id] == "🇯🇵JP_1|1.2.3.4", names
print("  ✓ 按地区+IP 规范化命名成功")

# 重复调用不应重复改名（序号稳定）
nodes = repository.get_nodes_by_ids([n1.id, n2.id])
mc.normalize_manual_names(nodes)
with repository.get_connection() as conn:
    names2 = {r["id"]: r["node_name"] for r in conn.execute("SELECT id,node_name FROM nodes")}
assert names2 == names, (names, names2)
print("  ✓ 幂等：重复规范化不改变名称")

# ---------- 6. 用户自定义名优先 ----------
print("\n=== 6. 用户自定义名优先 ===")
repository.rename_node(n1.id, "我的专属节点")
nodes = repository.get_nodes_by_ids([n1.id])
mc.normalize_manual_names(nodes)
with repository.get_connection() as conn:
    nm = conn.execute("SELECT node_name FROM nodes WHERE id=?", (n1.id,)).fetchone()["node_name"]
print("  名称:", nm)
assert nm == "我的专属节点", nm
print("  ✓ 自定义名未被规范化覆盖")

# ---------- 7. 缺地区的节点查询 ----------
print("\n=== 7. 缺地区节点查询 ===")
missing = repository.list_nodes_missing_geo_all(limit=100)
print("  缺地区节点:", [(m.id, m.node_name) for m in missing])
assert any(m.id == n3.id for m in missing)
print("  ✓ list_nodes_missing_geo_all 正确")

# ---------- 8. 空存活集 + 作用域（SQL 不能出现 NOT IN ()）----------
print("\n=== 8. 空存活集 + 作用域 ===")
# 手工测速全灭时的路径：active_fps 为空 → 作用域内全部降级，作用域外不动
repository.mark_nodes_status([n1.id], "active")
before = {}
with repository.get_connection() as conn:
    before = {r["id"]: r["status"] for r in conn.execute("SELECT id,status FROM nodes")}
n = repository.mark_missing_inactive(set(), scope_fingerprints=scope)
print("  降级数:", n, " 之前:", before)
with repository.get_connection() as conn:
    after = {r["id"]: r["status"] for r in conn.execute("SELECT id,status FROM nodes")}
print("  之后:", after)
assert after[n1.id] == "inactive", after
assert after[n4.id] == before[n4.id], "作用域外节点不应变化"
print("  ✓ 空存活集不报 SQL 错误，作用域外不受影响")

print("\n=== 全部断言通过 ===")
