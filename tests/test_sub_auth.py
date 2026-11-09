"""客户端订阅界面鉴权验证"""
import os
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

tmpdir = tempfile.mkdtemp(prefix="subauth_test_")
from src import config as cfg_mod
cfg_mod._config = {
    "app": {"name": "Sanl", "version": "test", "port": 8899},
    "database": {"path": os.path.join(tmpdir, "t.db").replace("\\", "/")},
    "gfw": {}, "scheduler": {}, "subs_check": {}, "sources": {},
}
from src.schema import repository
repository.init_db()

from fastapi.testclient import TestClient
from src.api.api import create_app
from src.auth import hash_password
repository.create_user("admin", hash_password("admin12345"), "admin")

app = create_app()
client = TestClient(app)

# ---------- 1. 未登录：指南页 302 跳主面板 ----------
print("=== 1. 未登录访问指南页 ===")
r = client.get("/client-guide.html", follow_redirects=False)
print("  ->", r.status_code, r.headers.get("location"))
assert r.status_code == 302, r.status_code
assert r.headers["location"] == "/"
print("  ✓ 未登录 302 → /")

# ---------- 2. 未登录：订阅内容接口 401 ----------
print("\n=== 2. 未登录拉订阅内容 ===")
r = client.get("/api/nodes/subscribe?fmt=clash")
print("  ->", r.status_code)
assert r.status_code == 401, r.status_code
print("  ✓ /api/nodes/subscribe 未登录 → 401（不再公开）")

# ---------- 3. 登录后：指南页 200 ----------
print("\n=== 3. 登录后访问指南页 ===")
r = client.post("/api/auth/login", json={"username": "admin", "password": "admin12345"})
assert r.status_code == 200
r = client.get("/client-guide.html", follow_redirects=False)
print("  ->", r.status_code)
assert r.status_code == 200
assert "客户端订阅指南" in r.text
assert "/sub/${token}/" in r.text or "/sub/" in r.text   # 页面脚本用 token 短链
assert "/api/nodes/subscribe?fmt=" not in r.text          # 旧的无鉴权链接已移除
print("  ✓ 登录后 200，页面脚本改用 /sub/ token 短链")

# ---------- 4. 登录后：/api/tokens/full 返回完整 token ----------
print("\n=== 4. 完整 Token 接口 ===")
r = client.get("/api/tokens/full")
print("  ->", r.status_code, r.json())
assert r.status_code == 200
# 未登录拿不到
c2 = TestClient(app)
r2 = c2.get("/api/tokens/full")
print("  未登录 ->", r2.status_code)
assert r2.status_code == 401
print("  ✓ 登录可见完整 token，未登录 401")

# ---------- 5. Token 短链仍然开放（客户端拉订阅） ----------
print("\n=== 5. Token 短链鉴权 ===")
t = repository.create_token(user_id=1, token_str="np_test_token_1234567890", name="guide-test")
r = client.get("/sub/np_test_token_1234567890/clash")
print("  有效 token ->", r.status_code)
assert r.status_code == 200 or r.status_code == 404, r.status_code   # 404=无可用节点，但鉴权通过
r = client.get("/sub/bad_token/clash")
print("  无效 token ->", r.status_code)
assert r.status_code == 401
print("  ✓ /sub/{token}/{fmt} 鉴权正常（这正是订阅的新入口）")

# ---------- 6. 管理面板其它公开读接口不受影响 ----------
print("\n=== 6. 公开接口回归 ===")
c3 = TestClient(app)
for path in ("/api/nodes/stats", "/api/health", "/api/version"):
    r = c3.get(path)
    print(f"  {path} -> {r.status_code}")
    assert r.status_code == 200, (path, r.status_code)
print("  ✓ 公开读接口不受影响")

print("\n=== 全部断言通过 ===")
