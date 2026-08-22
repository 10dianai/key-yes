#!/usr/bin/env python3
"""管理端点：池统计、Key 列表/启停/删除、批量导入、导出、面板登录。

鉴权两种凭证任选其一：
  1. 面板登录会话 token（X-Panel-Token 头，网页界面用）
  2. 配置里的 admin_key（X-Admin-Key 头，curl/脚本调用 API 用）
"""

import json
import logging
import zipfile

from fastapi import APIRouter, Request, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse

from core.key_importer import import_auto, import_bytes
from core.key_store import STATUS_ACTIVE, STATUS_DISABLED, STATUS_INVALID

ROUTER = APIRouter()
logger = logging.getLogger("keypool.admin")


async def require_admin(request: Request):
    """管理端点鉴权：面板 token 或 admin_key。均未配置时放行（由界面引导设密码）。"""
    app = request.app
    token = request.headers.get("x-panel-token", "")
    if token and app.state.panel_auth.token_valid(token):
        return
    admin_key = app.state.settings.admin_key
    if admin_key:
        provided = (
            request.headers.get("authorization", "").removeprefix("Bearer ").strip()
            or request.headers.get("x-admin-key", "")
        )
        if provided == admin_key:
            return
    elif not app.state.panel_auth.has_password():
        # 面板未初始化且未配置管理密钥：放行，由界面引导设置密码
        return
    raise HTTPException(status_code=401, detail="需要登录")


# ---- 面板密码端点（不要求已登录） ----

@ROUTER.get("/admin/panel/status")
async def panel_status(request: Request):
    return {"initialized": request.app.state.panel_auth.has_password()}


@ROUTER.post("/admin/panel/setup")
async def panel_setup(request: Request):
    """首次设置面板密码。已初始化后此端点失效，防止重置劫持。"""
    auth = request.app.state.panel_auth
    if auth.has_password():
        return JSONResponse({"error": "密码已初始化，请直接登录"}, 400)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "请求体不是合法 JSON"}, 400)
    try:
        auth.set_password(str(body.get("password") or ""))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, 400)
    logger.info("面板密码已初始化")
    return {"ok": True, "token": auth.create_token()}


@ROUTER.post("/admin/panel/login")
async def panel_login(request: Request):
    auth = request.app.state.panel_auth
    if not auth.has_password():
        return JSONResponse({"error": "面板未初始化，请先设置密码"}, 400)
    if not auth.login_allowed():
        return JSONResponse({"error": "失败次数过多，请 1 分钟后再试"}, 429)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "请求体不是合法 JSON"}, 400)
    if auth.check_password(str(body.get("password") or "")):
        logger.info("面板登录成功")
        # must_change：当前密码来自配置文件的明文（默认密码/管理员预设），
        # 提示前端强制要求修改
        return {
            "ok": True,
            "token": auth.create_token(),
            "must_change": bool(request.app.state.settings.panel_password),
        }
    auth.record_fail()
    logger.warning("面板登录失败（密码错误）")
    return JSONResponse({"error": "密码错误"}, 401)


@ROUTER.post("/admin/panel/change-password")
async def panel_change_password(request: Request):
    """修改面板密码（需已登录会话）。成功后自动清空配置文件里的明文密码，
    之后重启不会被配置覆盖。"""
    app = request.app
    auth = app.state.panel_auth
    token = request.headers.get("x-panel-token", "")
    if not (token and auth.token_valid(token)):
        return JSONResponse({"error": "需要登录"}, 401)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "请求体不是合法 JSON"}, 400)
    old_password = str(body.get("old_password") or "")
    new_password = str(body.get("new_password") or "")
    if not auth.check_password(old_password):
        return JSONResponse({"error": "旧密码错误"}, 401)
    try:
        auth.set_password(new_password)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, 400)

    # 清空配置来源文件里的 panel_password（防止下次重启被配置覆盖回去），
    # 同时更新内存里的 settings，否则本次运行期间 must_change 判断仍为 True
    _clear_config_password(app)
    object.__setattr__(app.state.settings, "panel_password", "")

    # 旧 token 失效，发新 token
    auth.revoke_token(token)
    logger.info("面板密码已修改（配置文件中的明文密码已清空）")
    return {"ok": True, "token": auth.create_token()}


def _clear_config_password(app):
    """把 panel_password 来源文件里的该字段清空（写回，保留其他字段）。"""
    source = app.state.settings.password_config_source
    try:
        data = json.loads(source.read_text(encoding="utf-8-sig"))
        if "panel_password" in data:
            data["panel_password"] = ""
            source.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("清空配置文件里的 panel_password 失败: %s", exc)


@ROUTER.post("/admin/panel/logout")
async def panel_logout(request: Request):
    request.app.state.panel_auth.revoke_token(
        request.headers.get("x-panel-token", ""))
    return {"ok": True}


# ---- 池管理端点（要求已登录） ----

@ROUTER.get("/admin/stats")
async def admin_stats(request: Request):
    await require_admin(request)
    return request.app.state.store.stats()


@ROUTER.get("/admin/keys")
async def admin_keys(request: Request, status: str = None):
    await require_admin(request)
    keys = request.app.state.store.list_keys(status=status)
    for item in keys:
        key = item["key"]
        item["key_masked"] = key[:6] + "..." + key[-4:] if len(key) > 12 else key
        item.pop("key", None)
    return {"keys": keys, "stats": request.app.state.store.stats()}


@ROUTER.post("/admin/import/path")
async def admin_import_path(request: Request):
    """本地路径导入：.txt / .zip / 文件夹 自动识别（4 种格式的本地入口）。"""
    await require_admin(request)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "请求体不是合法 JSON"}, 400)
    path = str(body.get("path") or "").strip().strip('"').strip("'")
    if not path:
        return JSONResponse({"error": "缺少 path 字段"}, 400)
    try:
        items, layout = import_auto(path)
    except (FileNotFoundError, NotADirectoryError, ValueError, OSError) as exc:
        return JSONResponse({"error": str(exc)}, 400)
    report = request.app.state.store.add_many(items)
    report["layout"] = layout
    report["total_parsed"] = len(items)
    logger.info("路径导入 %s: 新增%d 重复%d 无效%d",
                path, report["added"], report["duplicate"], report["invalid"])
    return report


@ROUTER.post("/admin/import/upload")
async def admin_import_upload(request: Request, file: UploadFile = File(...)):
    """上传导入：.txt 或 .zip（zip 内平铺 TXT 或文件夹嵌套 TXT 均可）。"""
    await require_admin(request)
    payload = await file.read()
    try:
        items, layout = import_bytes(file.filename, payload)
    except (ValueError, OSError, zipfile.BadZipFile) as exc:
        # BadZipFile 不是 OSError 子类，损坏/伪造的 zip 也要接住返回 400
        return JSONResponse({"error": f"无法解析上传文件: {exc}"}, 400)
    report = request.app.state.store.add_many(items)
    report["layout"] = layout
    report["total_parsed"] = len(items)
    logger.info("上传导入 %s: 新增%d 重复%d 无效%d",
                file.filename, report["added"], report["duplicate"], report["invalid"])
    return report


@ROUTER.post("/admin/keys/{key_id}/enable")
async def admin_enable(request: Request, key_id: str):
    await require_admin(request)
    entry = request.app.state.store.set_status(key_id, STATUS_ACTIVE)
    if not entry:
        return JSONResponse({"error": f"不存在: {key_id}"}, 404)
    return {"ok": True, "key": entry}


@ROUTER.post("/admin/keys/{key_id}/disable")
async def admin_disable(request: Request, key_id: str):
    await require_admin(request)
    entry = request.app.state.store.set_status(key_id, STATUS_DISABLED)
    if not entry:
        return JSONResponse({"error": f"不存在: {key_id}"}, 404)
    return {"ok": True, "key": entry}


@ROUTER.delete("/admin/keys/{key_id}")
async def admin_delete(request: Request, key_id: str):
    await require_admin(request)
    entry = request.app.state.store.delete(key_id)
    if not entry:
        return JSONResponse({"error": f"不存在: {key_id}"}, 404)
    return {"ok": True, "deleted": entry}


@ROUTER.post("/admin/clear")
async def admin_clear(request: Request):
    await require_admin(request)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        body = {}
    status = body.get("status")
    if status and status not in (STATUS_ACTIVE, STATUS_DISABLED, STATUS_INVALID):
        return JSONResponse({"error": f"无效状态: {status}"}, 400)
    removed = request.app.state.store.clear(status=status)
    logger.info("清空 status=%s：删除 %d 个", status, removed)
    return {"ok": True, "removed": removed}


@ROUTER.get("/admin/export")
async def admin_export(request: Request):
    """导出池内全部 Key 为纯 TXT（每行一个）。"""
    await require_admin(request)
    lines = [entry["key"] for entry in request.app.state.store.list_keys()]
    return PlainTextResponse("\n".join(lines) + ("\n" if lines else ""))
