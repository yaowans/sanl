"""手工测速两项修复验证：
1. hysteria2 节点进测速（clash-meta 喂入，不再被 generate_clash 过滤）
2. 规范命名带 IP 段：🇯🇵JP_1|49.212.231.67|452KB/s
"""
import os
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

tmpdir = tempfile.mkdtemp(prefix="mname_test_")
from src import config as cfg_mod
cfg_mod._config = {
    "app": {"name": "Sanl", "version": "test", "port": 8899},
    "database": {"path": os.path.join(tmpdir, "t.db").replace("\\", "/")},
    "gfw": {}, "scheduler": {}, "subs_check": {}, "sources": {},
}
from src.schema import repository
repository.init_db()

from src.manual_check import build_clash_yaml, normalize_manual_names

# ---------- 1. hysteria2 必须出现在 clash-meta 喂入内容里 ----------
print("=== 1. hysteria2 进测速内容 ===")
src = repository.get_or_create_manual_source("手动导入")
h1 = repository.add_node("manual://x", src.id, "AU01", "hysteria2",
                         {"server": "vpn-au-001.example.com", "port": 443,
                          "password": "pw", "sni": "vpn-au-001.example.com"},
                         status="active")
v1 = repository.add_node("manual://x", src.id, "JP01", "vless",
                         {"server": "49.212.231.67", "port": 443, "uuid": "u-1",
                          "tls": True}, status="active")
nodes = repository.get_nodes_by_ids([h1.id, v1.id])

yaml_body = build_clash_yaml(nodes)
print("  含 hysteria2 节点:", "type: hysteria2" in yaml_body)
assert "type: hysteria2" in yaml_body, "hysteria2 被过滤！"
assert "vpn-au-001.example.com" in yaml_body
assert "type: vless" in yaml_body
print("  ✓ clash-meta 喂入包含 hysteria2（基础 clash 会丢）")

# 基础 clash 确实会过滤（对照，证明这是修复点）
from src.api.subscribe import generate_by_format
basic = generate_by_format("clash", nodes)
assert "type: hysteria2" not in basic
print("  ✓ 对照：基础 clash 版确实过滤 hysteria2（旧 bug 复现）")

# ---------- 2. 规范命名带 IP 段 ----------
print("\n=== 2. 规范命名带 IP ===")
repository.update_node_geo(h1.id, "🇦🇺", "AU", "")
repository.update_node_geo(v1.id, "🇯🇵", "JP", "")
# v1 模拟有测速数据
repository.update_node_status(v1.id, "active", latency=50, download_speed=462848)

nodes = repository.get_nodes_by_ids([h1.id, v1.id])
renamed = normalize_manual_names(nodes)
print("  重命名:", renamed)
with repository.get_connection() as conn:
    names = {r["id"]: r["node_name"] for r in conn.execute("SELECT id,node_name FROM nodes")}
print("  名称:", names)
assert names[h1.id] == "🇦🇺AU_1|vpn-au-001.example.com", names
assert names[v1.id] == "🇯🇵JP_1|49.212.231.67|50ms", names
print("  ✓ 命名格式 {旗}{CC}_{i}|{IP}|{延迟}ms")

# ---------- 3. 速度段（无延迟时）----------
print("\n=== 3. 速度段格式 ===")
repository.update_node_status(v1.id, "active", latency=None, download_speed=462848)
nodes = repository.get_nodes_by_ids([v1.id])
normalize_manual_names(nodes)
with repository.get_connection() as conn:
    nm = conn.execute("SELECT node_name FROM nodes WHERE id=?", (v1.id,)).fetchone()[0]
print("  名称:", nm)
assert nm == "🇯🇵JP_1|49.212.231.67|452KB/s", nm
print("  ✓ 参考格式 🇯🇵JP_1|49.212.231.67|452KB/s 完全一致")

# ---------- 4. 带速度/延迟段的名字回填解析不丢指标 ----------
print("\n=== 4. 回填解析兼容 IP 段 ===")
from src.manual_check import _parse_subs_output
fake_yaml = """
proxies:
  - name: "🇯🇵JP_1|49.212.231.67|120ms|980KB/s"
    type: vless
    server: 49.212.231.67
    port: 443
    uuid: u-1
    tls: true
"""
import tempfile as tf
p = os.path.join(tmpdir, "all.yaml")
open(p, "w", encoding="utf-8").write(fake_yaml)
results = _parse_subs_output(p)
print("  解析结果:", [(r["node_name"], r["latency"], r["download_speed"]) for r in results])
assert results, "解析失败"
r0 = results[0]
assert r0["latency"] == 120, r0
assert r0["download_speed"] == int(980 * 1024), r0
assert r0["country"] == "🇯🇵", r0
print("  ✓ IP 段不干扰延迟/速度/国旗提取")

# ---------- 5. 自定义名仍然优先 ----------
print("\n=== 5. 自定义名优先 ===")
repository.rename_node(h1.id, "我的澳洲节点")
nodes = repository.get_nodes_by_ids([h1.id])
normalize_manual_names(nodes)
with repository.get_connection() as conn:
    nm = conn.execute("SELECT node_name FROM nodes WHERE id=?", (h1.id,)).fetchone()[0]
assert nm == "我的澳洲节点", nm
print("  ✓ 自定义名不被覆盖")

# ---------- 6. 域名原样（不做 DNS 解析）----------
print("\n=== 6. 域名服务器地址原样呈现 ===")
h2 = repository.add_node("manual://x", src.id, "US01", "ss",
                         {"server": "one.one.one.one", "port": 443,
                          "password": "p", "cipher": "aes-128-gcm"}, status="active")
repository.update_node_geo(h2.id, "🇺🇸", "US", "")
nodes = repository.get_nodes_by_ids([h2.id])
normalize_manual_names(nodes)
with repository.get_connection() as conn:
    nm = conn.execute("SELECT node_name FROM nodes WHERE id=?", (h2.id,)).fetchone()[0]
print("  名称:", nm)
assert nm == "🇺🇸US_1|one.one.one.one", nm   # 域名原样，不解析
print("  ✓ 域名原样呈现（CNAME/分线路域名不受境外 DNS 视角影响）")

# ---------- 7. 抓取回填不降级规范化名（用户报告的核心 bug）----------
print("\n=== 7. 抓取回填保留规范化名 ===")
# h2 已有规范化名（auto_name 戳）。模拟下一轮抓取带上游裸名入库：
repository.upsert_nodes_bulk([{
    "subscribe_url": "manual://x", "source_id": h2.source_id,
    "node_name": "上游裸名-US节点", "node_type": "ss",
    "node_data": {"server": "one.one.one.one", "port": 443,
                  "password": "p", "cipher": "aes-128-gcm"},
}])
with repository.get_connection() as conn:
    nm = conn.execute("SELECT node_name FROM nodes WHERE id=?", (h2.id,)).fetchone()[0]
print("  抓取后名称:", nm)
assert nm == "🇺🇸US_1|one.one.one.one", f"规范化名被上游裸名降级: {nm}"
print("  ✓ 抓取回填不再把规范化名冲回上游裸名")

# 测速回填同样不降级
repository.apply_check_results([{
    "node_type": "ss", "node_data": {"server": "one.one.one.one", "port": 443,
                                     "password": "p", "cipher": "aes-128-gcm"},
    "node_name": "subs-check-改名|980KB/s", "latency": 60, "download_speed": 1003520,
}])
with repository.get_connection() as conn:
    row = conn.execute("SELECT node_name, latency, download_speed FROM nodes WHERE id=?", (h2.id,)).fetchone()
print("  测速回填后:", row["node_name"], "lat =", row["latency"], "speed =", row["download_speed"])
assert row["node_name"] == "🇺🇸US_1|one.one.one.one", row["node_name"]
assert row["latency"] == 60 and row["download_speed"] == 1003520   # 指标照常更新
print("  ✓ 测速指标已更新、名字保留规范化格式（随后由全池 normalize 重建）")

print("\n=== 全部断言通过 ===")
