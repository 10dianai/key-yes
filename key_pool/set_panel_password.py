#!/usr/bin/env python3
"""手动设置/修改面板密码（服务运行中也可用，改完立即生效，无需重启）。

用法：
  python set_panel_password.py <新密码>          # 设置密码
  python set_panel_password.py                   # 交互式输入（推荐，密码不进命令行历史）

密码规则：至少 6 位。生成 PBKDF2-SHA256（20万轮）哈希写入 panel_auth.json。
服务运行中修改立即生效的原因：check_password 每次登录都现读文件。
"""

import getpass
import hashlib
import json
import secrets
import sys
from pathlib import Path

AUTH_FILE = Path(__file__).resolve().parent / "panel_auth.json"


def main():
    if len(sys.argv) > 2:
        print("用法: python set_panel_password.py [新密码]", file=sys.stderr)
        return 2
    password = sys.argv[1] if len(sys.argv) == 2 else getpass.getpass("输入新的面板密码: ")
    if len(password) < 6:
        print("[错误] 密码至少 6 位", file=sys.stderr)
        return 2

    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), 200_000
    ).hex()
    AUTH_FILE.write_text(
        json.dumps({"salt": salt, "hash": digest}), encoding="utf-8"
    )
    print(f"[完成] 面板密码已写入 {AUTH_FILE.name}（登录立即生效，无需重启服务）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
