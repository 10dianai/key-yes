"""API 集成测试（TestClient + mock 上游）：鉴权、三格式调用、导入、面板密码。"""

import io
import json
import zipfile

import pytest

POOL_KEY = "sk-pool-test"
ADMIN_KEY = "admin-test"
GOOD_KEY = "a" * 32            # mock 上游认它
BAD_KEY = "b" * 32             # mock 上游对它返回 401
TTS_VOICE = "en_paul_neutral"

POOL_H = {"Authorization": f"Bearer {POOL_KEY}"}
ADMIN_H = {"X-Admin-Key": ADMIN_KEY}
CLAUDE_H = {"x-api-key": POOL_KEY}
GEMINI_H = {"x-goog-api-key": POOL_KEY}


class TestAuth:
    def test_pool_key_required(self, client):
        r = client.post("/v1/chat/completions",
                        json={"model": "m", "messages": []})
        assert r.status_code == 401

    def test_wrong_pool_key_rejected(self, client):
        r = client.post("/v1/chat/completions",
                        headers={"Authorization": "Bearer wrong"},
                        json={"model": "m", "messages": []})
        assert r.status_code == 401

    def test_admin_key_required(self, client):
        assert client.get("/admin/stats").status_code == 401

    def test_healthz_open(self, client):
        assert client.get("/healthz").status_code == 200


class TestOpenAIGateway:
    def test_chat_non_stream(self, seeded_client):
        r = seeded_client.post("/v1/chat/completions", headers=POOL_H,
                        json={"model": "mistral-small-latest",
                              "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200
        assert r.json()["choices"][0]["message"]["content"] == "echo:hi"

    def test_chat_stream(self, seeded_client):
        with seeded_client.stream("POST", "/v1/chat/completions", headers=POOL_H,
                           json={"model": "m", "stream": True,
                                 "messages": [{"role": "user", "content": "ok"}]}) as r:
            assert r.status_code == 200
            text = ""
            for line in r.iter_lines():
                if line.startswith("data:"):
                    payload = line[5:].strip()
                    if payload not in ("", "[DONE]"):
                        d = json.loads(payload)
                        delta = (d.get("choices") or [{}])[0].get("delta", {})
                        text += delta.get("content") or ""
        assert text == "echo:ok"

    def test_models_list(self, seeded_client):
        r = seeded_client.get("/v1/models", headers=POOL_H)
        ids = [m["id"] for m in r.json()["data"]]
        assert "mistral-small-latest" in ids and "mistral-large-latest" in ids

    def test_models_cached(self, seeded_client):
        """模型列表自动获取上游并缓存：多次请求只打一次上游。"""
        transport = seeded_client.app.state.http._transport
        calls = {"n": 0}
        original_handler = transport.handler

        def counting(request):
            if request.url.path == "/v1/models":
                calls["n"] += 1
            return original_handler(request)

        transport.handler = counting
        for _ in range(3):
            r = seeded_client.get("/v1/models", headers=POOL_H)
            assert r.status_code == 200
        assert calls["n"] == 1, f"3 次请求应只调上游 1 次，实际 {calls['n']}"

    def test_models_cache_survives_empty_pool(self, seeded_client):
        """缓存过后空池也能返回模型列表（兜底）。"""
        r = seeded_client.get("/v1/models", headers=POOL_H)
        cached = [m["id"] for m in r.json()["data"]]
        seeded_client.app.state.store.clear()
        r = seeded_client.get("/v1/models", headers=POOL_H)
        assert [m["id"] for m in r.json()["data"]] == cached

    def test_models_auto_from_upstream(self, app):
        """models_list 为空时列表完全来自上游自动发现。"""
        object.__setattr__(app.state.settings, "models_list", ())
        app.state.store.add(GOOD_KEY)
        from fastapi.testclient import TestClient
        with TestClient(app) as c:
            r = c.get("/v1/models", headers=POOL_H)
            ids = [m["id"] for m in r.json()["data"]]
        assert "mistral-small-latest" in ids  # 来自上游而非配置
        assert "mistral-large-latest" in ids

    def test_models_claude_style(self, seeded_client):
        r = seeded_client.get("/v1/models", headers={"x-api-key": POOL_KEY})
        assert r.json()["data"][0]["type"] == "model"

    def test_missing_model(self, seeded_client):
        r = seeded_client.post("/v1/chat/completions", headers=POOL_H,
                        json={"messages": []})
        assert r.status_code == 400

    def test_empty_pool_503(self, app, client):
        r = client.post("/v1/chat/completions", headers=POOL_H,
                        json={"model": "m", "messages": []})
        assert r.status_code == 503

    def test_embeddings(self, seeded_client):
        r = seeded_client.post("/v1/embeddings", headers=POOL_H,
                        json={"model": "mistral-embed", "input": ["a", "b"]})
        assert r.status_code == 200 and len(r.json()["data"]) == 2

    def test_bad_key_rotation(self, seeded_client):
        """池里有坏 key：401 换 Key 重试后仍成功，坏 key 计失败。"""
        for _ in range(10):
            r = seeded_client.post("/v1/chat/completions", headers=POOL_H,
                            json={"model": "m",
                                  "messages": [{"role": "user", "content": "x"}]})
            assert r.status_code == 200
        bad = [k for k in seeded_client.app.state.store.list_keys()
               if k["label"] == "bad"]
        assert bad and bad[0]["fail_count"] >= 1


class TestClaudeGateway:
    def test_messages_non_stream(self, seeded_client):
        r = seeded_client.post("/v1/messages", headers=CLAUDE_H,
                        json={"model": "mistral-small-latest", "max_tokens": 10,
                              "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200
        body = r.json()
        assert body["content"][0]["text"] == "echo:hi"
        assert body["type"] == "message" and body["stop_reason"] == "end_turn"

    def test_messages_stream(self, seeded_client):
        with seeded_client.stream("POST", "/v1/messages", headers=CLAUDE_H,
                           json={"model": "m", "max_tokens": 10, "stream": True,
                                 "messages": [{"role": "user", "content": "ok"}]}) as r:
            assert r.status_code == 200
            text = ""
            for line in r.iter_lines():
                if line.startswith("data:"):
                    d = json.loads(line[5:])
                    if d.get("type") == "content_block_delta":
                        text += d["delta"].get("text") or ""
        assert text == "echo:ok"

    def test_count_tokens(self, client):
        r = client.post("/v1/messages/count_tokens", headers=CLAUDE_H,
                        json={"messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200 and r.json()["input_tokens"] > 0


class TestGeminiGateway:
    def test_generate_content(self, seeded_client):
        r = seeded_client.post("/v1beta/models/mistral-small-latest:generateContent",
                        headers=GEMINI_H,
                        json={"contents": [{"parts": [{"text": "hi"}]}]})
        assert r.status_code == 200
        body = r.json()
        assert body["candidates"][0]["content"]["parts"][0]["text"] == "echo:hi"

    def test_stream_generate_content(self, seeded_client):
        with seeded_client.stream(
            "POST",
            "/v1beta/models/mistral-small-latest:streamGenerateContent?alt=sse",
            headers=GEMINI_H,
            json={"contents": [{"parts": [{"text": "ok"}]}]},
        ) as r:
            assert r.status_code == 200
            text = ""
            for line in r.iter_lines():
                if line.startswith("data:"):
                    d = json.loads(line[5:])
                    parts = (d.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
                    text += "".join(p.get("text", "") for p in parts)
        assert text == "echo:ok"

    def test_models_list(self, seeded_client):
        r = seeded_client.get("/v1beta/models", headers=GEMINI_H)
        names = [m["name"] for m in r.json()["models"]]
        assert "models/mistral-small-latest" in names

    def test_invalid_action(self, seeded_client):
        r = seeded_client.post("/v1beta/models/mistral-small-latest:bogusAction",
                        headers=GEMINI_H, json={})
        assert r.status_code == 400


class TestTTS:
    def test_speech_returns_mp3(self, seeded_client):
        r = seeded_client.post("/v1/audio/speech", headers=POOL_H,
                        json={"input": "hello", "voice": TTS_VOICE})
        assert r.status_code == 200
        assert r.headers["content-type"] == "audio/mpeg"
        assert r.content == b"FAKE_MP3_BYTES"

    def test_default_voice_and_model_filled(self, seeded_client):
        r = seeded_client.post("/v1/audio/speech", headers=POOL_H, json={"input": "x"})
        assert r.status_code == 200

    def test_voices_list(self, seeded_client):
        r = seeded_client.get("/v1/audio/voices", headers=POOL_H)
        assert r.status_code == 200
        assert r.json()["items"][0]["slug"] == TTS_VOICE


class TestAdminImport:
    def _zip_bytes(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr("压缩包/u@792792.xyz.txt", GOOD_KEY + "\n")
        return buffer.getvalue()

    def test_upload_zip(self, client, admin_headers):
        r = client.post("/admin/import/upload", headers=admin_headers,
                        files={"file": ("t.zip", self._zip_bytes(),
                                        "application/zip")})
        assert r.status_code == 200
        body = r.json()
        assert body["added"] == 1 and body["duplicate"] == 0

    def test_upload_dedup(self, client, admin_headers):
        for _ in range(2):
            r = client.post("/admin/import/upload", headers=admin_headers,
                            files={"file": ("t.zip", self._zip_bytes(),
                                            "application/zip")})
        assert r.json()["added"] == 0 and r.json()["duplicate"] == 1

    def test_upload_broken_zip_400(self, client, admin_headers):
        r = client.post("/admin/import/upload", headers=admin_headers,
                        files={"file": ("bad.zip", b"PK\x03\x04 garbage",
                                        "application/zip")})
        assert r.status_code == 400

    def test_upload_rejects_exe(self, client, admin_headers):
        r = client.post("/admin/import/upload", headers=admin_headers,
                        files={"file": ("v.exe", b"MZ...", "application/octet-stream")})
        assert r.status_code == 400

    def test_import_path_txt(self, client, admin_headers, tmp_path):
        path = tmp_path / "keys.txt"
        path.write_text(GOOD_KEY + "\n", encoding="utf-8")
        r = client.post("/admin/import/path", headers=admin_headers,
                        json={"path": str(path)})
        assert r.json()["added"] == 1


class TestAdminKeys:
    def test_list_masked(self, seeded_client, admin_headers):
        r = seeded_client.get("/admin/keys", headers=admin_headers)
        keys = r.json()["keys"]
        assert len(keys) == 2
        assert all("key" not in k and "key_masked" in k for k in keys)

    def test_disable_enable_delete(self, seeded_client, admin_headers):
        keys = seeded_client.get("/admin/keys", headers=admin_headers).json()["keys"]
        key_id = keys[0]["id"]
        assert seeded_client.post(f"/admin/keys/{key_id}/disable",
                           headers=admin_headers).status_code == 200
        assert seeded_client.post(f"/admin/keys/{key_id}/enable",
                           headers=admin_headers).status_code == 200
        assert seeded_client.delete(f"/admin/keys/{key_id}",
                             headers=admin_headers).status_code == 200
        assert seeded_client.delete(f"/admin/keys/{key_id}",
                             headers=admin_headers).status_code == 404

    def test_export(self, seeded_client, admin_headers):
        r = seeded_client.get("/admin/export", headers=admin_headers)
        lines = [line for line in r.text.splitlines() if line.strip()]
        assert sorted(lines) == sorted([GOOD_KEY, BAD_KEY])

    def test_clear_invalid(self, seeded_client, admin_headers):
        store = seeded_client.app.state.store
        # mark_result 需要池内真实引用（pick 返回的即内部 dict），list_keys 是拷贝
        bad_entry = None
        for _ in range(10):
            entry = store.pick()
            if entry and entry["label"] == "bad":
                bad_entry = entry
                break
        assert bad_entry is not None
        for _ in range(3):
            store.mark_result(bad_entry, ok=False)
        r = seeded_client.post("/admin/clear", headers=admin_headers,
                        json={"status": "invalid"})
        assert r.json()["removed"] == 1
        assert store.stats()["total"] == 1


class TestPanelAuth:
    def test_full_lifecycle(self, client):
        # 未初始化
        assert client.get("/admin/panel/status").json() == {"initialized": False}
        # 弱密码被拒
        r = client.post("/admin/panel/setup", json={"password": "123"})
        assert r.status_code == 400
        # 设置密码拿 token
        r = client.post("/admin/panel/setup", json={"password": "panel-pass-123"})
        assert r.status_code == 200 and r.json()["token"]
        token = r.json()["token"]
        # 二次 setup 被拒（防重置劫持）
        r = client.post("/admin/panel/setup", json={"password": "hacked-12345"})
        assert r.status_code == 400
        # token 可访问管理接口
        r = client.get("/admin/stats", headers={"X-Panel-Token": token})
        assert r.status_code == 200
        # 伪造 token 拒绝
        assert client.get("/admin/stats",
                          headers={"X-Panel-Token": "fake"}).status_code == 401
        # 错误密码
        r = client.post("/admin/panel/login", json={"password": "wrong"})
        assert r.status_code == 401
        # 正确密码
        r = client.post("/admin/panel/login", json={"password": "panel-pass-123"})
        assert r.status_code == 200
        # 登出后 token 失效
        token2 = r.json()["token"]
        client.post("/admin/panel/logout", headers={"X-Panel-Token": token2})
        assert client.get("/admin/stats",
                          headers={"X-Panel-Token": token2}).status_code == 401

    def test_brute_force_lockout(self, client):
        client.post("/admin/panel/setup", json={"password": "panel-pass-123"})
        for _ in range(5):
            client.post("/admin/panel/login", json={"password": "wrong"})
        r = client.post("/admin/panel/login", json={"password": "panel-pass-123"})
        assert r.status_code == 429


class TestStaticUI:
    def test_index_serves_html(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "Mistral Key 池管理" in r.text
        assert "admin.js" in r.text

    def test_admin_js_served(self, client):
        r = client.get("/static/admin.js")
        assert r.status_code == 200
        assert "X-Panel-Token" in r.text
