"""GFW 配置 API 端到端验证（本地起 app + TestClient）"""
import os
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

tmpdir = tempfile.mkdtemp(prefix="gfwapi_test_")
db_path = os.path.join(tmpdir, "t.db").replace("\\", "/")

from src import config as cfg_mod
cfg_mod._config = {
    "app": {"name": "Sanl", "version": "test", "port": 8899},
    "database": {"path": db_path},
    "gfw": {"enabled": False, "probe_url": "", "probe_key": "",
            "timeout": 3, "batch_size": 100},
    "scheduler": {"qualified_latency_ms": 800},
    "subs_check": {},
    "sources": {},
}
for k in ("CN_PROBE_URL", "CN_PROBE_KEY", "CN_PROBE_ENABLED"):
    os.environ.pop(k, None)

from fastapi.testclient import TestClient
from src.api.api import create_app
from src.schema import repository

repository.init_db()

# 建管理员账号
from src.auth import hash_password
repository.create_user("admin", hash_password("admin12345"), "admin")

app = create_app()
client = TestClient(app)

# 登录拿会话
r = client.post("/api/auth/login", json={"username": "admin", "password": "admin12345"})
print("login:", r.status_code, r.text[:200])
assert r.status_code == 200, r.text

# --- GET /api/gfw/config ---
r = client.get("/api/gfw/config")
print("GET config:", r.status_code)
assert r.status_code == 200, r.text
d = r.json()
keys = [i["key"] for i in d["items"]]
print("fields:", keys)
assert "gfw.enabled" in keys and "gfw.probe_url" in keys
assert d["probe_ready"] is False
# 字段元数据齐全（前端据此渲染说明）
for it in d["items"]:
    assert it["label"] and it["help"] and it["type"], it
print("✓ GET /api/gfw/config 返回字段元数据+说明")

# --- 未登录访问应 401 ---
c2 = TestClient(app)
r2 = c2.get("/api/gfw/config")
print("unauth GET:", r2.status_code)
assert r2.status_code == 401, r2.text
print("✓ 未登录访问配置接口 → 401")

# --- POST /api/gfw/config 保存 ---
r = client.post("/api/gfw/config", json={"values": {
    "gfw.enabled": True,
    "gfw.probe_url": "http://probe.example:8899",
    "gfw.probe_key": "secret-key-1234",
    "gfw.timeout": 4,
}})
print("POST config:", r.status_code)
assert r.status_code == 200, r.text
d = r.json()
items = {i["key"]: i for i in d["items"]}
assert items["gfw.probe_url"]["value"] == "http://probe.example:8899", items["gfw.probe_url"]
assert items["gfw.probe_url"]["source"] == "db"
assert items["gfw.probe_key"]["source"] == "db"
assert "secret-key-1234" not in str(items["gfw.probe_key"]["value"])
print("✓ POST /api/gfw/config 保存成功，密码脱敏")

# 落库验证
rows = repository.get_app_settings()
print("db overrides:", rows)
assert rows["gfw.probe_url"] == "http://probe.example:8899"
assert rows["gfw.probe_key"] == "secret-key-1234"
print("✓ 配置已落库 app_settings 表")

# --- 再 GET 确认生效 ---
r = client.get("/api/gfw/config")
items = {i["key"]: i for i in r.json()["items"]}
assert items["gfw.timeout"]["value"] == 4.0, items["gfw.timeout"]
print("✓ 重新读取生效值正确")

# --- POST /api/gfw/config/reset ---
r = client.post("/api/gfw/config/reset", json={"keys": []})
print("POST reset:", r.status_code)
assert r.status_code == 200, r.text
items = {i["key"]: i for i in r.json()["items"]}
assert items["gfw.probe_url"]["source"] != "db", items["gfw.probe_url"]
assert items["gfw.enabled"]["value"] is False
print("✓ POST /api/gfw/config/reset 清除覆盖并回落")

# --- GET /api/gfw/config/probe-script ---
r = client.get("/api/gfw/config/probe-script")
print("GET probe-script:", r.status_code)
assert r.status_code == 200, r.text
d = r.json()
assert "cn_probe" in d["content"], d["content"][:100]
assert len(d["usage"]) > 0
print(f"✓ GET /api/gfw/config/probe-script 返回探针脚本 ({len(d['content'])} 字符)")

# --- POST /api/gfw/config/test（地址不可达，应返回 error 但 200）---
r = client.post("/api/gfw/config/test", json={"values": {
    "gfw.probe_url": "http://127.0.0.1:59999", "gfw.probe_key": "k"}})
print("POST config/test:", r.status_code, r.text[:200])
assert r.status_code == 200, r.text
d = r.json()
assert d["status"] == "error" and not d["ok"], d
print("✓ POST /api/gfw/config/test 对不可达地址返回明确错误")

# --- POST /api/gfw/config 空 values → 400 ---
r = client.post("/api/gfw/config", json={"values": {}})
print("POST empty:", r.status_code)
assert r.status_code == 400, r.text
print("✓ 空 values → 400")

print("\n=== API 端到端验证全部通过 ===")
