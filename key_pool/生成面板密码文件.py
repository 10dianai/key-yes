#!/usr/bin/env python3
"""在客户服务器上生成面板密码文件（不联网、不上传文件、立即生效）。

使用方法（复制到客户服务器终端执行）：

  1. 把下面【改这里】的密码换成你要设置的密码
  2. 整段复制粘贴到客户服务器的终端里回车
  3. 执行重启命令（见文件底部说明）
  4. 客户刷新面板页面，用新密码登录

适用于：Docker 部署或直接部署，任何版本，无需更新代码。
"""

import hashlib
import json
import secrets

# ============ 改这里 ============
新密码 = "改成你要设置的密码"        # 至少 6 位
输出路径 = "panel_auth.json"        # 生成到哪里（当前目录）
# ================================

if len(新密码) < 6:
    raise SystemExit("[错误] 密码至少 6 位")

盐值 = secrets.token_hex(16)
哈希 = hashlib.pbkdf2_hmac(
    "sha256", 新密码.encode("utf-8"), bytes.fromhex(盐值), 200_000
).hex()

with open(输出路径, "w", encoding="utf-8") as 文件:
    json.dump({"salt": 盐值, "hash": 哈希}, 文件)

print(f"[完成] 已生成 {输出路径}")
print()
print("=" * 50)
print("下一步（在客户服务器上执行重启）：")
print()
print("  Docker 部署：")
print("    docker cp panel_auth.json 容器名:/data/panel_auth.json")
print("    docker restart 容器名")
print()
print("  直接部署（panel_auth.json 生成在服务目录下时）：")
print("    重启服务进程即可")
print("=" * 50)
