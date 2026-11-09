"""客户端订阅指南页路由验证"""
import os
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

tmpdir = tempfile.mkdtemp(prefix="cg_test_")
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
client = TestClient(create_app())

r = client.get("/client-guide.html")
print("GET /client-guide.html ->", r.status_code, r.headers.get("content-type"))
assert r.status_code == 200, r.status_code
assert "客户端订阅指南" in r.text, r.text[:200]
assert "<!DOCTYPE html>" in r.text
assert "Sanl - 客户端订阅指南" in r.text
# 确认返回的是指南页而非 SPA fallback 的主面板 index.html
assert "login-overlay" not in r.text, "返回的是主面板 index.html（SPA fallback 吞掉了路由）"
print("✓ /client-guide.html 返回独立指南页（未被 SPA fallback 吞掉）")

# 未登录也应放行（鉴权中间件白名单）
print("✓ 鉴权中间件放行（上面 200 即证明）")

print("\n=== 全部断言通过 ===")
