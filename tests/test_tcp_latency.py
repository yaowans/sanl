"""真实 TCP 延迟测量 + 假延迟清除验证"""
import asyncio
import os
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

tmpdir = tempfile.mkdtemp(prefix="lat_test_")
from src import config as cfg_mod
cfg_mod._config = {
    "app": {"name": "Sanl", "version": "test", "port": 8899},
    "database": {"path": os.path.join(tmpdir, "t.db").replace("\\", "/")},
    "gfw": {}, "scheduler": {}, "subs_check": {}, "sources": {},
}
from src.schema import repository
repository.init_db()

# ---------- 1. 解析器不再造 50ms 假延迟 ----------
print("=== 1. 解析器不造假延迟 ===")
import yaml
fake_yaml = """
proxies:
  - name: "🇺🇸US_1|685KB/s"
    type: ss
    server: 1.2.3.4
    port: 443
    cipher: aes-128-gcm
    password: pw
"""
p = os.path.join(tmpdir, "all.yaml")
open(p, "w", encoding="utf-8").write(fake_yaml)

from src.manual_check import _parse_subs_output
res = _parse_subs_output(p)
print("  解析结果:", [(r["node_name"], r["latency"], r["download_speed"]) for r in res])
assert res and res[0]["latency"] is None, f"仍造假延迟: {res[0]['latency']}"
assert res[0]["download_speed"] == 685 * 1024
print("  ✓ 只有速度时 latency=None（不再反推成 50ms）")

# ---------- 2. 真实 TCP 测量 ----------
print("\n=== 2. 真实 TCP 握手延迟 ===")
from src.utils.tcplatency import measure_tcp_latency


async def run_measure():
    # 本地起一个 TCP 监听，测量应得到个位数 ms
    async def handler(reader, writer):
        try:
            writer.close()
        except Exception:
            pass

    srv = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    try:
        out = await measure_tcp_latency([(1, "127.0.0.1", port)], concurrency=4, timeout=2)
    finally:
        srv.close()
        await srv.wait_closed()
    return out

out = asyncio.run(run_measure())
print("  测量结果:", out)
assert out.get(1) and out[1].get("rtt") is not None, out
assert 0 <= out[1]["rtt"] < 2000, out
assert out[1]["edge"] is False, out   # 127.0.0.1 不是 CDN
print(f"  ✓ 真实测量到 RTT = {out[1]['rtt']}ms")

# 不可达目标返回 None
out2 = asyncio.run(measure_tcp_latency([(2, "127.0.0.1", 1)], concurrency=2, timeout=1))
print("  不可达:", out2)
assert out2.get(2, {}).get("rtt") is None, out2
print("  ✓ 不可达返回 None（不编造数值）")

# CDN 网段识别
from src.utils.tcplatency import is_cdn_ip
assert is_cdn_ip("104.21.93.88") is True
assert is_cdn_ip("172.67.1.1") is True
assert is_cdn_ip("92.223.71.239") is False
print("  ✓ CDN 边缘网段识别正确")

# ---------- 3. 批量写库 ----------
print("\n=== 3. 批量写入延迟 ===")
src = repository.get_or_create_manual_source("t")
n1 = repository.add_node("manual://t", src.id, "A", "ss",
                         {"server": "127.0.0.1", "port": 1, "password": "p",
                          "cipher": "aes-128-gcm"}, status="active")
n2 = repository.add_node("manual://t", src.id, "B", "ss",
                         {"server": "127.0.0.1", "port": 2, "password": "p",
                          "cipher": "aes-128-gcm"}, status="active")
# 模拟旧假数据
repository.update_node_status(n1.id, "active", latency=50, download_speed=1000)
repository.update_node_status(n2.id, "active", latency=50, download_speed=1000)
written = repository.update_node_latencies({
    n1.id: {"rtt": 123, "edge": True},
    n2.id: {"rtt": None, "edge": False},
})
print("  写入条数:", written)
assert written == 1, written   # rtt=None 不写入
assert repository.get_node(n1.id).latency == 123
assert repository.get_node(n2.id).latency == 50   # None 保持原值（不清空）
# CDN 标记写入 node_data
d1 = repository.get_node(n1.id).node_data
assert isinstance(d1, dict) and d1.get("latency_edge") == 1, d1
print("  ✓ 只写非 None 值，CDN 边缘标记已写入 node_data")

print("\n=== 全部断言通过 ===")
