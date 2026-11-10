"""定时调度配置 API 验证"""
import os
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 用临时 yaml 隔离（set 接口会写 config/app.yaml）
tmpdir = tempfile.mkdtemp(prefix="sched_test_")
yaml_path = os.path.join(tmpdir, "app.yaml")
with open(yaml_path, "w") as f:
    f.write("app: {name: Sanl, port: 8899}\nscheduler: {fetch_cron: '0 7,14,18 * * *', check_cron: '0 7,14,18 * * *'}\n")

from src import config as cfg_mod
# 覆盖 _CONFIG_PATH 指向临时文件
from src import config
config._CONFIG_PATH = yaml_path
cfg_mod._config = None

from src.schema import repository
repository.init_db()

from fastapi.testclient import TestClient
from src.api.api import create_app
from src.auth import hash_password
repository.create_user("admin", hash_password("admin12345"), "admin")

client = TestClient(create_app())
r = client.post("/api/auth/login", json={"username": "admin", "password": "admin12345"})
assert r.status_code == 200

# ---------- 1. GET 当前配置 ----------
print("=== 1. GET 当前配置 ===")
r = client.get("/api/settings/schedule")
print("  ", r.json())
assert r.status_code == 200
d = r.json()
assert d["fetch_cron"] == "0 7,14,18 * * *"
assert d["check_cron"] == "0 7,14,18 * * *"
print("  ✓ 读取默认 cron")

# ---------- 2. POST 合法表达式 ----------
print("\n=== 2. 保存合法 cron ===")
r = client.post("/api/settings/schedule", params={
    "fetch_cron": "0 */3 * * *", "check_cron": "*/20 * * * *"})
print("  ", r.json())
assert r.status_code == 200, r.text
d = r.json()
assert d["rescheduled"] in (True, False)   # 测试环境 scheduler=None → False
assert d["fetch_cron"] == "0 */3 * * *"

# 写回验证（读临时 yaml）
import yaml
cfg = yaml.safe_load(open(yaml_path))
assert cfg["scheduler"]["fetch_cron"] == "0 */3 * * *"
assert cfg["scheduler"]["check_cron"] == "*/20 * * * *"
print("  ✓ yaml 已写回")

# 再 GET 确认运行时读到新值
r = client.get("/api/settings/schedule")
assert r.json()["fetch_cron"] == "0 */3 * * *", r.json()
print("  ✓ 运行时读取到新值（reload_config 生效）")

# ---------- 3. 非法表达式 → 400 ----------
print("\n=== 3. 非法表达式 ===")
for bad in ("只有四段 * * * *", "abc def ghi jkl mno"):
    r = client.post("/api/settings/schedule", params={"check_cron": bad})
    print(f"  {bad!r} -> {r.status_code}: {r.json().get('detail','')[:60]}")
    assert r.status_code == 400, (bad, r.status_code)
print("  ✓ 非法表达式被拒")

# ---------- 4. 空 params → 400 ----------
r = client.post("/api/settings/schedule")
assert r.status_code == 400
print("\n=== 4. 空 params → 400 ✓")

# ---------- 5. 未登录 → 401 ----------
c2 = TestClient(create_app())
r = c2.get("/api/settings/schedule")
assert r.status_code == 401, r.status_code
print("=== 5. 未登录 GET → 401 ✓")

print("\n=== 全部断言通过 ===")
