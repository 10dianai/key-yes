#!/usr/bin/env python3
"""日志配置：控制台 + 滚动文件（logs/key_pool.log，10MB x 5 个备份）。"""

import logging
from logging.handlers import RotatingFileHandler

from .config import Settings

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(settings: Settings, debug: bool = False) -> logging.Logger:
    """初始化根日志器，返回应用主 logger（名字 keypool）。

    可重复调用（测试里多次构造 app）：已有 handler 会先清掉。
    """
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.setLevel(logging.INFO)
    root.addHandler(console)

    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        settings.logs_dir / "key_pool.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # 压掉 uvicorn 访问日志的噪音（关键错误仍会到 error logger）
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    return logging.getLogger("keypool")
