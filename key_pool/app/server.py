#!/usr/bin/env python3
"""FastAPI 应用工厂：路由注册、静态资源、lifespan、请求日志中间件。

create_app(settings) 组装全部依赖并返回 FastAPI 实例——
测试里用临时目录配置即可构造完整应用。
"""

import logging
import pathlib
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from core.key_store import KeyStore
from .admin_api import ROUTER as ADMIN_ROUTER
from .gateway import ROUTER as GATEWAY_ROUTER
from .panel_auth import PanelAuth
from .upstream import UpstreamClient

logger = logging.getLogger("keypool.app")

STATIC_DIR = pathlib.Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = app.state.settings
    # 测试可能已注入带 mock transport 的客户端；只有未注入时才新建
    http_client = getattr(app.state, "http", None)
    if http_client is None:
        proxy = settings.upstream_proxy or None
        http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=15.0,
                read=settings.request_timeout_seconds,
                write=60.0,
                pool=15.0,
            ),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
            proxy=proxy,
        )
        app.state.http = http_client
    app.state.upstream = UpstreamClient(settings, app.state.store, http_client)
    logger.info("上游客户端就绪: %s %s",
                settings.upstream_base_url,
                f"（代理 {settings.upstream_proxy}）" if settings.upstream_proxy else "（直连）")
    yield
    await http_client.aclose()
    app.state.store.close()
    logger.info("已关闭上游客户端并落盘池数据")


def create_app(settings, store: KeyStore = None, panel_auth: PanelAuth = None) -> FastAPI:
    """构造完整应用。

    Args:
        settings: app.config.Settings
        store / panel_auth: 测试时可注入自定义实例；缺省按 settings 新建。
    """
    app = FastAPI(
        title="Mistral Key Pool",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.store = store or KeyStore(
        settings.data_file,
        logger=logging.getLogger("keypool.store"),
    )
    app.state.panel_auth = panel_auth or PanelAuth(settings.panel_auth_file)

    # 请求日志中间件（跳过 /healthz 噪音）
    @app.middleware("http")
    async def access_log(request: Request, call_next):
        if request.url.path == "/healthz":
            return await call_next(request)
        started = time.monotonic()
        response = await call_next(request)
        elapsed = int((time.monotonic() - started) * 1000)
        logging.getLogger("keypool.http").info(
            "%s %s -> %d %dms",
            request.method, request.url.path, response.status_code, elapsed,
        )
        return response

    # 池访问鉴权：所有 /v1、/v1beta 调用端点统一走 FastAPI 依赖
    from fastapi import Depends
    from .auth import require_pool_auth
    app.include_router(GATEWAY_ROUTER, dependencies=[Depends(require_pool_auth)])
    app.include_router(ADMIN_ROUTER)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC_DIR / "admin.html")

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "keys": app.state.store.stats()}

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        logger.exception("未处理异常: %s %s", request.method, request.url.path)
        return JSONResponse({"error": f"服务内部错误: {exc}"}, status_code=500)

    return app
