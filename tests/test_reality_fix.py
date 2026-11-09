"""Reality 参数字段名兼容测试

问题：Clash 源用 reality-opts: {public-key, short-id}，
      内部导出只认 {pbk, sid} → 导出的 vless 链接缺 reality 参数 → 客户端连不上。
"""
import os
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api.importer import _from_clash_proxies
from src.api.subscribe import _to_link, _reality_opts, generate_by_format
from src.schema.models import Node

# ---------- 1. Clash 导入时归一化 ----------
print("=== 1. Clash 导入归一化 ===")
clash_proxies = [{
    "name": "日本-1号",
    "type": "vless",
    "server": "49.212.231.67",
    "port": 443,
    "uuid": "uuid-1",
    "tls": True,
    "servername": "dyn.felissimo.co.jp",
    "flow": "xtls-rprx-vision",
    "client-fingerprint": "chrome",
    "reality-opts": {"public-key": "PBK_VALUE", "short-id": "SID_VALUE"},
}]
out = _from_clash_proxies(clash_proxies)
assert len(out) == 1
ptype, data, name = out[0]
print("  归一化后 reality-opts:", data.get("reality-opts"))
assert data["reality-opts"]["pbk"] == "PBK_VALUE", data
assert data["reality-opts"]["sid"] == "SID_VALUE", data
print("  ✓ public-key/short-id → pbk/sid")

# ---------- 2. sing-box 格式（本来就是 pbk/sid）不能被破坏 ----------
print("\n=== 2. sing-box 格式保持不变 ===")
sb = [{"name": "sb", "type": "vless", "server": "1.2.3.4", "port": 443, "uuid": "u",
       "tls": {"enabled": True, "server_name": "x.com",
               "reality": {"enabled": True, "public_key": "SB_PBK", "short_id": "SB_SID"}}}]
from src.api.importer import _from_singbox_outbounds
sb_out = _from_singbox_outbounds(sb)
assert sb_out, sb_out
print("  sing-box reality-opts:", sb_out[0][1].get("reality-opts"))
assert sb_out[0][1]["reality-opts"]["pbk"] == "SB_PBK"
print("  ✓ sing-box 路径未受影响")

# ---------- 3. 导出链接必须带 reality 参数 ----------
print("\n=== 3. 导出链接带 reality 参数 ===")
n = Node(id=1, node_name="日本-1号", node_type="vless",
         node_data={"server": "49.212.231.67", "port": 443, "uuid": "uuid-1",
                    "tls": True, "servername": "dyn.felissimo.co.jp",
                    "flow": "xtls-rprx-vision",
                    "reality-opts": {"public-key": "PBK_VALUE", "short-id": "SID_VALUE"}})
link = _to_link(n)
print("  链接:", link)
assert "security=reality" in link, link
assert "pbk=PBK_VALUE" in link, link
assert "sid=SID_VALUE" in link, link
print("  ✓ Clash 格式的 reality-opts 正确导出为 pbk/sid")

# ---------- 4. sing-box 格式的内部存储也要能导出 ----------
print("\n=== 4. 内部 pbk/sid 格式导出 ===")
n2 = Node(id=2, node_name="x", node_type="vless",
          node_data={"server": "1.2.3.4", "port": 443, "uuid": "u", "tls": True,
                     "reality-opts": {"pbk": "PBK2", "sid": "SID2"}})
link2 = _to_link(n2)
print("  链接:", link2)
assert "pbk=PBK2" in link2 and "sid=SID2" in link2, link2
print("  ✓ 内部格式同样正确导出")

# ---------- 5. Clash YAML 导出保留 reality-opts ----------
print("\n=== 5. Clash YAML 导出 ===")
y = generate_by_format("clash", [n])
print("  含 reality-opts:", "reality-opts" in y)
assert "reality-opts" in y, y[:400]
assert "PBK_VALUE" in y and "SID_VALUE" in y, y[:600]
print("  ✓ Clash YAML 导出保留 public-key/short-id")

# ---------- 6. sing-box 导出保留 reality ----------
print("\n=== 6. sing-box 导出 ===")
import json
sbj = json.loads(generate_by_format("singbox", [n]))
outb = sbj["outbounds"][0]
print("  outbound tls:", outb.get("tls"))
assert outb.get("tls", {}).get("reality", {}).get("public_key") == "PBK_VALUE", outb
assert outb.get("tls", {}).get("reality", {}).get("short_id") == "SID_VALUE", outb
print("  ✓ sing-box 导出保留 reality 参数")

# ---------- 7. 无 reality 的节点不受影响 ----------
print("\n=== 7. 无 reality 节点不受影响 ===")
n3 = Node(id=3, node_name="plain", node_type="vless",
          node_data={"server": "5.6.7.8", "port": 443, "uuid": "u", "tls": True})
link3 = _to_link(n3)
print("  链接:", link3)
assert "security=tls" in link3, link3
assert "pbk" not in link3, link3
print("  ✓ 普通 TLS 节点不受影响")

print("\n=== 全部断言通过 ===")
