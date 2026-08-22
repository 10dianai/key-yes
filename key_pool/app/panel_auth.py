#!/usr/bin/env python3
"""面板密码：首次进入初始化、PBKDF2 哈希存储、会话 token、失败限流。

以类实例提供（依赖注入），不再依赖模块级全局变量。
"""

import hashlib
import json
import secrets
import time
from pathlib import Path

TOKEN_TTL_SECONDS = 12 * 3600   # 登录会话 12 小时
MAX_FAIL = 5                    # 连续失败 5 次
LOCK_SECONDS = 60               # 锁定 60 秒
PBKDF2_ROUNDS = 200_000


class PanelAuth:
    def __init__(self, auth_file: Path):
        self.auth_file = Path(auth_file)
        self._sessions = {}    # token -> 过期时间戳
        self._fail_times = []  # 最近登录失败时间戳（限流用）

    # ---- 密码存储 ----

    def has_password(self) -> bool:
        """已初始化 = 文件存在且内容是合法的 {salt, hash}。

        空文件（如部署时 touch 出来的）不算已初始化，
        这样首屏仍会进入"初始化面板"流程而不是死锁在登录框。
        """
        if not self.auth_file.exists():
            return False
        try:
            data = json.loads(self.auth_file.read_text(encoding="utf-8-sig"))
            return bool(data.get("salt") and data.get("hash"))
        except (OSError, json.JSONDecodeError, ValueError):
            return False

    def _hash(self, password: str, salt_hex: str) -> str:
        return hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), PBKDF2_ROUNDS
        ).hex()

    def set_password(self, password: str) -> None:
        if len(password) < 6:
            raise ValueError("密码至少 6 位")
        salt = secrets.token_hex(16)
        self.auth_file.parent.mkdir(parents=True, exist_ok=True)
        self.auth_file.write_text(
            json.dumps({"salt": salt, "hash": self._hash(password, salt)}),
            encoding="utf-8",
        )

    def check_password(self, password: str) -> bool:
        try:
            data = json.loads(self.auth_file.read_text(encoding="utf-8-sig"))
            return secrets.compare_digest(
                self._hash(password, data["salt"]), data["hash"]
            )
        except (OSError, json.JSONDecodeError, KeyError, ValueError):
            return False

    # ---- 会话 token ----

    def create_token(self) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        for stale in [t for t, exp in self._sessions.items() if exp < now]:
            self._sessions.pop(stale, None)
        self._sessions[token] = now + TOKEN_TTL_SECONDS
        return token

    def token_valid(self, token: str) -> bool:
        expiry = self._sessions.get(token)
        if not expiry:
            return False
        if time.time() > expiry:
            self._sessions.pop(token, None)
            return False
        return True

    def revoke_token(self, token: str) -> None:
        self._sessions.pop(token, None)

    # ---- 登录限流 ----

    def login_allowed(self) -> bool:
        now = time.time()
        self._fail_times[:] = [
            t for t in self._fail_times if now - t < LOCK_SECONDS
        ]
        return len(self._fail_times) < MAX_FAIL

    def record_fail(self) -> None:
        self._fail_times.append(time.time())
