"""pytest 公共夹具：临时目录配置、mock 上游、TestClient。"""

import json
import sys
from pathlib import Path

import httpx
import pytest

# 保证能导入 key_pool 包内的 app/core
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.server import create_app  # noqa: E402
from core.key_store import KeyStore  # noqa: E402
from app.panel_auth import PanelAuth  # noqa: E402

POOL_KEY = "sk-pool-test"
ADMIN_KEY = "admin-test"
GOOD_KEY = "a" * 32            # mock 上游认它
BAD_KEY = "b" * 32             # mock 上游对它返回 401
TTS_VOICE = "en_paul_neutral"


def make_settings(tmp_path, **overrides) -> Settings:
    """构造指向临时目录的配置。"""
    fields = dict(
        host="127.0.0.1", port=8787,
        upstream_base_url="http://mock-upstream",
        upstream_proxy="",
        pool_api_keys=(POOL_KEY,),
        admin_key=ADMIN_KEY,
        panel_password="",
        default_model="mistral-small-latest",
        models_list=("mistral-small-latest",),
        key_retry_on_rate_limit=3,
        request_timeout_seconds=10.0,
        data_file=tmp_path / "pool_data.json",
        config_dir=tmp_path,
        panel_auth_file=tmp_path / "panel_auth.json",
        logs_dir=tmp_path / "logs",
        password_config_source=tmp_path / "key_pool_config.json",
    )
    fields.update(overrides)
    return Settings(**fields)


class MockUpstream:
    """模拟 Mistral 上游：chat 流式/非流式、models、speech、voices、embeddings。

    401 逻辑：Bearer BAD_KEY 返回 401（验证换 Key 重试），其余 Bearer 放行。
    """

    def __call__(self, request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        path = request.url.path
        if auth == f"Bearer {BAD_KEY}" or not auth.startswith("Bearer "):
            return httpx.Response(401, json={"object": "error", "message": "invalid api key"})

        if path == "/v1/models":
            return httpx.Response(200, json={"object": "list", "data": [
                {"id": "mistral-small-latest"}, {"id": "mistral-large-latest"}]})

        if path == "/v1/chat/completions":
            body = json.loads(request.content or b"{}")
            user_text = ""
            for m in body.get("messages", []):
                if m.get("role") == "user":
                    content = m.get("content")
                    user_text = content if isinstance(content, str) else "img"
            if body.get("stream"):
                stream_text = f"echo:{user_text}"
                # 用 SSE 文本模拟流式
                sse = ""
                for token in stream_text:
                    chunk = {"id": "c1", "object": "chat.completion.chunk",
                             "choices": [{"index": 0, "delta": {"content": token},
                                          "finish_reason": None}]}
                    sse += "data: " + json.dumps(chunk) + "\n\n"
                done = {"id": "c1", "choices": [{"index": 0, "delta": {},
                                                 "finish_reason": "stop"}]}
                usage = {"id": "c1", "usage": {"prompt_tokens": 5, "completion_tokens": 4,
                                               "total_tokens": 9}}
                sse += "data: " + json.dumps(done) + "\n\n"
                sse += "data: " + json.dumps(usage) + "\n\n"
                sse += "data: [DONE]\n\n"
                return httpx.Response(200, text=sse,
                                      headers={"content-type": "text/event-stream"})
            return httpx.Response(200, json={
                "id": "c1", "object": "chat.completion", "model": body.get("model"),
                "choices": [{"index": 0,
                             "message": {"role": "assistant", "content": f"echo:{user_text}"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}})

        if path == "/v1/embeddings":
            count = len(json.loads(request.content or b"{}").get("input") or [])
            return httpx.Response(200, json={
                "object": "list", "data": [
                    {"object": "embedding", "index": i, "embedding": [0.1, 0.2]}
                    for i in range(count)]})

        if path == "/v1/audio/speech":
            import base64
            return httpx.Response(200, json={
                "audio_data": base64.b64encode(b"FAKE_MP3_BYTES").decode()})

        if path == "/v1/audio/voices":
            return httpx.Response(200, json={"items": [
                {"slug": TTS_VOICE, "name": "Paul", "languages": ["en_us"]}]})

        return httpx.Response(404, json={"object": "error", "message": f"unknown {path}"})


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
def app(settings):
    """带 mock 上游的完整应用实例（预注入 mock transport，lifespan 会沿用）。"""
    import httpx as _httpx
    from app.server import create_app as _create
    from app.upstream import UpstreamClient

    application = _create(settings)
    application.state.http = _httpx.AsyncClient(
        transport=httpx.MockTransport(MockUpstream()))
    application.state.upstream = UpstreamClient(
        settings, application.state.store, application.state.http)
    return application


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def seeded_client(app):
    """预置好/坏 Key 的池 + 已启动的 TestClient。"""
    app.state.store.add(GOOD_KEY, label="good")
    app.state.store.add(BAD_KEY, label="bad")
    from fastapi.testclient import TestClient
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def pool_headers():
    return {"Authorization": f"Bearer {POOL_KEY}"}


@pytest.fixture
def admin_headers():
    return {"X-Admin-Key": ADMIN_KEY}


@pytest.fixture
def seeded_store(settings):
    """预置 GOOD_KEY + BAD_KEY 的池。"""
    store = KeyStore(settings.data_file, start_flusher=False)
    store.add(GOOD_KEY, label="good")
    store.add(BAD_KEY, label="bad")
    return store
