"""GFW 配置可配置化 —— 隔离验证

验证：
1. 优先级：DB 覆盖 > 环境变量 > app.yaml > 默认值
2. 保存后立即生效（缓存失效）
3. 复位回落
4. 密码字段脱敏
5. 类型转换与范围钳制
6. 非法键被忽略
"""
import os
import sys
import tempfile

# Windows 控制台默认 GBK，✓/中文会抛 UnicodeEncodeError
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import settings
from src.schema import repository


def main():
    tmpdir = tempfile.mkdtemp(prefix="gfwcfg_test_")
    db_path = os.path.join(tmpdir, "t.db").replace("\\", "/")

    # 用临时库隔离：直接替换 config 模块缓存 + init_db
    from src import config as cfg_mod
    cfg_mod._config = {
        "app": {"name": "Sanl", "version": "test"},
        "database": {"path": db_path},
        "gfw": {
            "enabled": False,
            "probe_url": "http://yaml.example:8899",
            "probe_key": "yaml-key",
            "timeout": 5,
            "batch_size": 200,
        },
    }
    repository.init_db()

    os.environ.pop("CN_PROBE_URL", None)
    os.environ.pop("CN_PROBE_KEY", None)
    os.environ.pop("CN_PROBE_ENABLED", None)

    # --- 1. app.yaml 生效 ---
    cfg = settings.resolve_gfw_config()
    assert cfg["gfw.probe_url"] == "http://yaml.example:8899", cfg
    assert cfg["gfw.probe_url__source"] == "yaml", cfg
    assert cfg["gfw.timeout"] == 5.0, cfg
    # 未在 yaml 里的字段回落默认值
    assert cfg["gfw.check_limit"] == 5000, cfg
    assert cfg["gfw.check_limit__source"] == "default", cfg
    print("✓ 1. app.yaml 生效 / 默认值回落")

    # --- 2. 环境变量优先于 yaml ---
    os.environ["CN_PROBE_URL"] = "http://env.example:8899"
    os.environ["CN_PROBE_ENABLED"] = "true"
    settings.invalidate_settings_cache()
    cfg = settings.resolve_gfw_config()
    assert cfg["gfw.probe_url"] == "http://env.example:8899", cfg
    assert cfg["gfw.probe_url__source"] == "env", cfg
    assert cfg["gfw.enabled"] is True, cfg
    print("✓ 2. 环境变量优先于 app.yaml")

    # --- 3. DB 覆盖优先于环境变量 ---
    payload = settings.save_gfw_settings({
        "gfw.probe_url": "http://db.example:8899",
        "gfw.probe_key": "db-secret",
        "gfw.timeout": 8,
        "gfw.enabled": True,
        "gfw.filter_subscription": False,
    })
    cfg = settings.resolve_gfw_config()
    assert cfg["gfw.probe_url"] == "http://db.example:8899", cfg
    assert cfg["gfw.probe_url__source"] == "db", cfg
    assert cfg["gfw.probe_key"] == "db-secret", cfg
    assert cfg["gfw.timeout"] == 8.0, cfg
    assert cfg["gfw.enabled"] is True, cfg
    assert cfg["gfw.filter_subscription"] is False, cfg
    # 未覆盖的字段仍走环境变量
    assert cfg["gfw.batch_size"] == 200.0, cfg
    assert cfg["gfw.batch_size__source"] == "yaml", cfg
    print("✓ 3. DB 覆盖优先于环境变量，未覆盖项保持原来源")

    # --- 4. 密码脱敏 ---
    item = next(i for i in payload["items"] if i["key"] == "gfw.probe_key")
    assert "db-secret" not in str(item["value"]), item
    assert "••••" in str(item["value"]), item
    print("✓ 4. 密码字段脱敏:", item["value"])

    # --- 5. 立即生效（不重启进程）---
    settings.save_gfw_settings({"gfw.probe_url": "http://db2.example:8899"})
    cfg = settings.resolve_gfw_config()
    assert cfg["gfw.probe_url"] == "http://db2.example:8899", cfg
    print("✓ 5. 保存后立即生效")

    # --- 6. 数值钳制 + 类型转换 ---
    settings.save_gfw_settings({"gfw.timeout": 999, "gfw.batch_size": 0.1})
    cfg = settings.resolve_gfw_config()
    assert cfg["gfw.timeout"] == 10.0, cfg          # 上限 10
    assert cfg["gfw.batch_size"] == 1.0, cfg        # 下限 1
    print("✓ 6. 数值范围钳制生效")

    # --- 7. 非法键被忽略 ---
    before = settings.resolve_gfw_config()
    settings.save_gfw_settings({"gfw.not_exist": "x", "app.api_key": "evil"})
    after = settings.resolve_gfw_config()
    assert before["gfw.probe_url"] == after["gfw.probe_url"], (before, after)
    print("✓ 7. 非白名单键被忽略")

    # --- 8. 复位回落 ---
    settings.reset_gfw_settings()
    cfg = settings.resolve_gfw_config()
    assert cfg["gfw.probe_url"] == "http://env.example:8899", cfg
    assert cfg["gfw.probe_url__source"] == "env", cfg
    assert cfg["gfw.filter_subscription"] is True, cfg
    print("✓ 8. 复位后回落到环境变量 / app.yaml")

    # --- 9. gfw_probe_enabled 完整性判断 ---
    # 清掉 yaml 与环境变量来源，确保"空"是真缺失而非回落
    cfg_mod._config["gfw"]["probe_key"] = ""
    cfg_mod._config["gfw"]["probe_url"] = ""
    os.environ.pop("CN_PROBE_URL", None)
    os.environ.pop("CN_PROBE_KEY", None)
    settings.save_gfw_settings({"gfw.enabled": True, "gfw.probe_url": "http://x", "gfw.probe_key": ""})
    settings.invalidate_settings_cache()
    cfg = settings.resolve_gfw_config()
    assert not cfg["gfw.probe_key"], cfg
    assert settings.gfw_probe_enabled() is False, "空密钥应判定为不可用"
    # 缺地址同样不可用
    settings.save_gfw_settings({"gfw.probe_url": "", "gfw.probe_key": "k"})
    settings.invalidate_settings_cache()
    cfg = settings.resolve_gfw_config()
    assert not cfg["gfw.probe_url"], cfg
    assert settings.gfw_probe_enabled() is False, "空地址应判定为不可用"
    # 两者齐全才可用
    settings.save_gfw_settings({"gfw.probe_url": "http://x", "gfw.probe_key": "k"})
    assert settings.gfw_probe_enabled() is True
    print("✓ 9. 探针完整性判断（地址+密钥缺一不可）")

    # --- 10. 空字符串 = 清除该项覆盖 ---
    os.environ["CN_PROBE_URL"] = "http://env.example:8899"
    settings.save_gfw_settings({"gfw.probe_url": ""})
    settings.invalidate_settings_cache()
    cfg = settings.resolve_gfw_config()
    assert cfg["gfw.probe_url"] == "http://env.example:8899", cfg
    assert cfg["gfw.probe_url__source"] == "env", cfg
    print("✓ 10. 空字符串清除覆盖并回落")

    print("\n=== 全部断言通过 ===")


if __name__ == "__main__":
    main()
