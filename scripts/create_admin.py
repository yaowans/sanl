#!/usr/bin/env python3
"""创建/重置 Sanl 管理员账号。

用法：
    python3 scripts/create_admin.py <username>              # 交互式输入密码
    python3 scripts/create_admin.py <username> <password>    # 直接指定密码
    python3 scripts/create_admin.py <username> --reset       # 重置已有账号的密码
"""
import os
import sys
import getpass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from src.schema import repository  # noqa: E402
from src.auth import hash_password  # noqa: E402


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    username = sys.argv[1].strip()
    reset = "--reset" in sys.argv[2:]

    password = None
    for a in sys.argv[2:]:
        if a != "--reset":
            password = a
            break
    if password is None:
        password = getpass.getpass("密码 (至少8位): ")
        confirm = getpass.getpass("确认密码: ")
        if password != confirm:
            print("两次密码不一致")
            sys.exit(1)

    if len(password) < 8:
        print("密码至少 8 位")
        sys.exit(1)

    repository.init_db()

    existing = repository.get_user_by_username(username)
    if existing:
        if not reset:
            print(f"账号 {username} 已存在。加 --reset 可重置密码。")
            sys.exit(1)
        repository.update_user_password(existing.id, hash_password(password))
        repository.update_user_role(existing.id, "admin")
        repository.delete_user_sessions(existing.id)
        print(f"已重置 {username} 的密码并设为 admin")
    else:
        u = repository.create_user(username, hash_password(password), "admin")
        if not u:
            print(f"创建失败：账号 {username} 可能已存在")
            sys.exit(1)
        print(f"已创建管理员 {username}")


if __name__ == "__main__":
    main()
