#!/usr/bin/env python3
"""配置加载与启动校验。

合并顺序：key_pool_config.json -> key_pool_config.local.json -> 命令行覆盖。
校验失败抛 ConfigError，错误信息直接可操作（中文）。
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_FILE = "key_pool_config.json"
LOCAL_CONFIG_FILE = "key_pool_config.local.json"


class ConfigError(ValueError):
    """配置不合法（文件缺失/JSON 损坏/字段类型或取值错误）。"""


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    upstream_base_url: str
    upstream_proxy: str
    pool_api_keys: tuple
    admin_key: str
    default_model: str
    models_list: tuple
    key_retry_on_rate_limit: int
    request_timeout_seconds: float
    data_file: Path
    config_dir: Path
    panel_auth_file: Path
    logs_dir: Path

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


def _as_int(value, field_name, default, minimum=None, maximum=None):
    if value is None or value == "":
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"配置项 {field_name} 必须是数字，当前是 {value!r}")
    number = int(value)
    if minimum is not None and number < minimum:
        raise ConfigError(f"配置项 {field_name} 不能小于 {minimum}，当前是 {number}")
    if maximum is not None and number > maximum:
        raise ConfigError(f"配置项 {field_name} 不能大于 {maximum}，当前是 {number}")
    return number


def _as_url(value, field_name, default):
    if not value:
        return default
    if not isinstance(value, str) or not value.startswith(("http://", "https://")):
        raise ConfigError(
            f"配置项 {field_name} 必须以 http:// 或 https:// 开头，当前是 {value!r}"
        )
    return value.rstrip("/")


def load_config(config_file=DEFAULT_CONFIG_FILE) -> Settings:
    path = Path(config_file)
    if not path.exists():
        raise ConfigError(
            f"配置文件不存在: {path}（在 key_pool/ 目录下运行，或用 --config 指定路径）"
        )
    try:
        cfg = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件不是合法 JSON: {path}（第 {exc.lineno} 行: {exc.msg}）") from exc

    local = path.parent / LOCAL_CONFIG_FILE
    if local.exists():
        try:
            cfg.update(json.loads(local.read_text(encoding="utf-8-sig")))
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"本地覆盖配置 {local.name} 不是合法 JSON（第 {exc.lineno} 行: {exc.msg}）"
            ) from exc

    pool_keys = cfg.get("pool_api_keys")
    if pool_keys is None:
        pool_keys = []
    if not isinstance(pool_keys, list) or not all(isinstance(k, str) for k in pool_keys):
        raise ConfigError("配置项 pool_api_keys 必须是字符串数组")
    admin_key = cfg.get("admin_key")
    if admin_key is not None and not isinstance(admin_key, str):
        raise ConfigError("配置项 admin_key 必须是字符串")

    models = cfg.get("models_list")
    if models is None:
        models = []
    if not isinstance(models, list) or not all(isinstance(m, str) for m in models):
        raise ConfigError("配置项 models_list 必须是字符串数组")

    config_dir = path.parent
    return Settings(
        host=str(cfg.get("host") or "127.0.0.1"),
        port=_as_int(cfg.get("port"), "port", 8787, minimum=1, maximum=65535),
        upstream_base_url=_as_url(cfg.get("upstream_base_url"),
                                  "upstream_base_url", "https://api.mistral.ai"),
        upstream_proxy=_as_url(cfg.get("upstream_proxy"), "upstream_proxy", "") or "",
        pool_api_keys=tuple(k.strip() for k in pool_keys if k.strip()),
        admin_key=str(admin_key or "").strip(),
        default_model=str(cfg.get("default_model") or "mistral-small-latest"),
        models_list=tuple(models),
        key_retry_on_rate_limit=_as_int(
            cfg.get("key_retry_on_rate_limit"), "key_retry_on_rate_limit", 2,
            minimum=0, maximum=20),
        request_timeout_seconds=float(_as_int(
            cfg.get("request_timeout_seconds"), "request_timeout_seconds", 300,
            minimum=1, maximum=86400)),
        data_file=config_dir / str(cfg.get("data_file") or "pool_data.json"),
        config_dir=config_dir,
        panel_auth_file=config_dir / "panel_auth.json",
        logs_dir=config_dir / "logs",
    )
