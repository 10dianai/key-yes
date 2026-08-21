#!/usr/bin/env python3
"""鉴权工具：调用方密钥（池访问）与管理员凭证的提取与校验。"""

from fastapi import HTTPException, Request


def extract_caller_key(request: Request) -> str:
    """兼容三种鉴权头：Bearer / x-api-key / x-goog-api-key / ?key=。"""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    for header in ("x-api-key", "x-goog-api-key"):
        value = request.headers.get(header)
        if value:
            return value.strip()
    return request.query_params.get("key", "")


def require_pool_auth(request: Request) -> None:
    """池访问鉴权；pool_api_keys 为空表示本地放开。"""
    keys = request.app.state.settings.pool_api_keys
    if not keys:
        return
    provided = extract_caller_key(request)
    if provided not in keys:
        raise HTTPException(status_code=401, detail="无效的池访问密钥")
