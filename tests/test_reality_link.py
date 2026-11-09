"""vless reality 链接解析验证（根因 A：分支顺序 bug）"""
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api.importer import parse_vless

# 带 sni 的 reality 链接（机场订阅的常见形态）
uri = ("vless://4bd60ce2-1e47-4895-9694-f678e6c1726d@49.212.231.67:443"
       "?security=reality&sni=dyn.felissimo.co.jp&flow=xtls-rprx-vision"
       "&pbk=IdeH5iV40a-ogW35yVj3JbpDNigXXMeYfhuGNEYTK1o&sid=01fba6f6"
       "&fp=chrome#日本-1号")
t, d, name = parse_vless(uri)
print("type:", t, " name:", name)
print("data:", d)
assert d.get("reality-opts", {}).get("pbk") == "IdeH5iV40a-ogW35yVj3JbpDNigXXMeYfhuGNEYTK1o", d
assert d.get("reality-opts", {}).get("sid") == "01fba6f6", d
assert d.get("sni") == "dyn.felissimo.co.jp", d
assert d.get("flow") == "xtls-rprx-vision", d
assert d.get("client-fingerprint") == "chrome", d
print("✓ reality 链接（带 sni）的 pbk/sid 正确提取")

# 普通 tls 链接不受影响
uri2 = "vless://uuid-x@1.2.3.4:443?security=tls&sni=example.com#普通"
t2, d2, _ = parse_vless(uri2)
assert "reality-opts" not in d2, d2
assert d2.get("sni") == "example.com", d2
assert d2.get("tls") is True, d2
print("✓ 普通 tls 链接不受影响")

# 无 sni 的 reality 链接
uri3 = "vless://uuid-y@5.6.7.8:443?security=reality&pbk=PBK3&sid=SID3#r3"
t3, d3, _ = parse_vless(uri3)
assert d3.get("reality-opts", {}).get("pbk") == "PBK3", d3
print("✓ 无 sni 的 reality 链接正常")

print("\n=== 全部断言通过 ===")
