#!/usr/bin/env python3
"""Mistral Key 号池网关服务入口。

用法：
  python run.py                # 常规启动（读 key_pool_config.json）
  python run.py --dev          # 开发模式（代码改动自动重载）
  python run.py --port 9000    # 覆盖端口
  python run.py --config xx.json
"""

import argparse
import os
import socket
import sys
from pathlib import Path

# 保证从任意工作目录启动都能找到 app/core 包
sys.path.insert(0, str(Path(__file__).resolve().parent))

import uvicorn

from app.config import ConfigError, load_config
from app.logging_setup import setup_logging
from app.server import create_app


def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        return sock.connect_ex((host, port)) == 0


def port_hint(host: str, port: int) -> str:
    if sys.platform == "win32":
        return (
            f"  排查命令：netstat -ano | findstr :{port}\n"
            f"  找到 PID 后结束它：taskkill /F /PID <PID>\n"
            f"  （常见原因：旧的服务进程还活着）"
        )
    return (
        f"  排查命令：lsof -i :{port}\n"
        f"  找到 PID 后结束它：kill <PID>\n"
        f"  （常见原因：旧的服务进程还活着）"
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description="Mistral Key 号池网关服务")
    parser.add_argument("--config", default="key_pool_config.json",
                        help="配置文件路径（默认 key_pool_config.json）")
    parser.add_argument("--host", default=None, help="覆盖配置中的 host")
    parser.add_argument("--port", type=int, default=None, help="覆盖配置中的 port")
    parser.add_argument("--dev", action="store_true", help="开发模式：代码改动自动重载")
    args = parser.parse_args(argv)

    try:
        settings = load_config(args.config)
    except ConfigError as exc:
        if "不存在" not in str(exc):
            print(f"[配置错误] {exc}", file=sys.stderr)
            return 2
        # 首次启动：生成默认配置再加载（Docker 数据卷挂载场景）
        from app.config import ensure_default_config
        ensure_default_config(Path(args.config))
        print(f"[首次启动] 已生成默认配置 {args.config}（面板默认密码 admin123，"
              f"登录后强制修改；国内服务器需在配置里补 upstream_proxy）", flush=True)
        settings = load_config(args.config)
    # 覆盖优先级：命令行参数 > 环境变量（容器部署用 HOST/PORT）> 配置文件。
    # 容器里必须监听 0.0.0.0，否则端口映射的流量进不到容器的 loopback。
    env_host = os.environ.get("HOST")
    env_port = os.environ.get("PORT")
    host = args.host or env_host
    port = args.port or (int(env_port) if env_port and env_port.isdigit() else None)
    if host:
        object.__setattr__(settings, "host", host)
    if port:
        object.__setattr__(settings, "port", port)

    # data_file 若是目录（Docker 挂载文件但宿主机没建文件时会变文件夹），明确报错
    if settings.data_file.is_dir():
        print(
            f"[启动失败] 数据文件 {settings.data_file} 是一个文件夹。\n"
            f"  原因：Docker 按文件挂载时宿主机文件不存在会自动创建同名文件夹。\n"
            f"  修复：宿主机执行 rm -rf {settings.data_file.name} && "
            f"touch {settings.data_file.name} 后重启容器；\n"
            f"  或改用目录挂载（docker-compose.yml 里挂 ./data:/data）。",
            file=sys.stderr,
        )
        return 3

    logger = setup_logging(settings, debug=args.dev)

    if not args.dev and port_in_use(settings.host, settings.port):
        print(f"[启动失败] 端口 {settings.host}:{settings.port} 已被占用", file=sys.stderr)
        print(port_hint(settings.host, settings.port), file=sys.stderr)
        return 3

    app = create_app(settings)

    print("=" * 56, flush=True)
    print(f"  Mistral Key 池网关启动: {settings.base_url}", flush=True)
    print(f"  管理界面:   {settings.base_url}/", flush=True)
    if not app.state.panel_auth.has_password():
        print("  面板未初始化: 首次打开管理界面会引导设置密码", flush=True)
    print(f"  OpenAI 格式: POST /v1/chat/completions  GET /v1/models", flush=True)
    print(f"  Claude 格式: POST /v1/messages          POST /v1/messages/count_tokens", flush=True)
    print(f"  Gemini 格式: POST /v1beta/models/<model>:generateContent", flush=True)
    print(f"               POST /v1beta/models/<model>:streamGenerateContent", flush=True)
    print(f"  TTS:         POST /v1/audio/speech       STT: POST /v1/audio/transcriptions", flush=True)
    print(f"  上游: {settings.upstream_base_url}"
          + (f"（代理 {settings.upstream_proxy}）" if settings.upstream_proxy else "（直连）"),
          flush=True)
    print(f"  池内 Key: {app.state.store.stats()['total']}   日志: {settings.logs_dir / 'key_pool.log'}",
          flush=True)
    print("=" * 56, flush=True)

    if args.dev:
        uvicorn.run("app.server:dev_app_factory", host=settings.host,
                    port=settings.port, reload=True,
                    reload_dirs=[str(Path(__file__).parent)], log_level="info")
    else:
        uvicorn.run(app, host=settings.host, port=settings.port, log_level="warning")
    return 0


def dev_app_factory():
    """--dev 模式下 uvicorn reload 需要的模块级工厂。"""
    settings = load_config("key_pool_config.json")
    setup_logging(settings, debug=True)
    return create_app(settings)


if __name__ == "__main__":
    raise SystemExit(main())
