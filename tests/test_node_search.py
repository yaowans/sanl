"""节点全库搜索 + anytls 订阅输出验证"""
import os
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

tmpdir = tempfile.mkdtemp(prefix="search_test_")
from src import config as cfg_mod
cfg_mod._config = {
    "app": {"name": "Sanl", "version": "test", "port": 8899},
    "database": {"path": os.path.join(tmpdir, "t.db").replace("\\", "/")},
    "gfw": {}, "scheduler": {}, "subs_check": {}, "sources": {},
}
from src.schema import repository
repository.init_db()

src = repository.get_or_create_manual_source("测试源")

# 构造 120 个节点：其中一个 US_22 在"后面几页"
nodes = []
for i in range(120):
    server = f"10.0.{i // 250}.{i % 250 + 1}"
    nodes.append(repository.add_node(
        "manual://t", src.id, f"🇺🇸US_{i}", "ss",
        {"server": server, "port": 443, "password": f"pw{i}", "cipher": "aes-128-gcm"},
        status="active"))
special = repository.add_node(
    "manual://t", src.id, "🇺🇸US_22|dlyqv8kg.jhm.qwer123.net|50ms", "ss",
    {"server": "dlyqv8kg.jhm.qwer123.net", "port": 36608,
     "password": "268c7249-f84e-4771-a137-ca79caed8c94", "cipher": "aes-128-gcm"},
    status="active")
print(f"构造节点 {len(nodes) + 1} 个，特殊节点 id={special.id}")

# ---------- 1. 全库搜索（keyword） ----------
print("\n=== 1. 全库搜索 ===")
page1 = repository.list_nodes(limit=50, offset=0)
print("  第 1 页:", len(page1), "个，含目标节点:", any(n.id == special.id for n in page1))
assert not any(n.id == special.id for n in page1), "目标节点不应在第一页"

hits = repository.list_nodes(limit=500, keyword="dlyqv8kg")
print("  keyword=dlyqv8kg 命中:", [(n.id, n.node_name[:40]) for n in hits])
assert len(hits) == 1 and hits[0].id == special.id
print("  ✓ 全库搜索能命中非第一页的节点")

# 按节点名前缀搜索
hits2 = repository.list_nodes(limit=500, keyword="US_22")
assert any(n.id == special.id for n in hits2), [n.node_name for n in hits2][:5]
print("  ✓ 按名字搜索命中")

# 无命中
assert repository.list_nodes(limit=500, keyword="不存在的关键词xyz") == []
print("  ✓ 无命中返回空")

# ---------- 2. anytls 订阅输出 ----------
print("\n=== 2. anytls 节点进订阅 ===")
anytls = repository.add_node(
    "manual://t", src.id, "🇺🇸US_9|usgc.aikunapp.com|50ms", "anytls",
    {"server": "usgc.aikunapp.com", "port": 6001, "password": "18a4197e",
     "sni": "st.gcore.com", "skip-cert-verify": True, "type": "anytls"},
    status="active")
repository.update_node_status(anytls.id, "active", latency=50, download_speed=100000)

from src.api.subscribe import generate_links, generate_by_format, _to_link

link = _to_link(repository.get_node(anytls.id))
print("  anytls 链接:", link)
assert link.startswith("anytls://18a4197e@usgc.aikunapp.com:6001"), link
assert "sni=st.gcore.com" in link, link
print("  ✓ anytls 生成合法 URI")

# txt 订阅包含 anytls 节点
sub_nodes = repository.list_nodes(limit=500)
txt = generate_links(sub_nodes)
assert "anytls://" in txt, txt[:300]
print("  ✓ txt/base64 订阅包含 anytls")

# clash-meta 含 anytls
meta = generate_by_format("clash-meta", [repository.get_node(anytls.id)])
assert "type: anytls" in meta, meta[:400]
assert "18a4197e" in meta, meta[:400]
print("  ✓ clash-meta 输出 anytls 代理")

# 基础 clash 跳过 anytls（不支持）
basic = generate_by_format("clash", [repository.get_node(anytls.id)])
assert "type: anytls" not in basic, basic[:300]
print("  ✓ 基础 clash 跳过 anytls（协议不支持）")

# sing-box 含 anytls
import json
sb = json.loads(generate_by_format("singbox", [repository.get_node(anytls.id)]))
types = [o.get("type") for o in sb["outbounds"]]
assert "anytls" in types, types
print("  ✓ sing-box 输出 anytls outbound:", types)

# 未知协议不再产出 type:ss 假节点
fake = repository.add_node("manual://t", src.id, "怪协议", "weirdproto",
                           {"server": "1.2.3.4", "port": 443}, status="active")
meta2 = generate_by_format("clash-meta", [repository.get_node(fake.id)])
assert "weirdproto" not in meta2 and "1.2.3.4" not in meta2, meta2[:400]
print("  ✓ 未知协议跳过，不再产出字段错乱的假 ss 节点")

print("\n=== 全部断言通过 ===")
