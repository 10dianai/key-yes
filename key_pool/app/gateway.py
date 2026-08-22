#!/usr/bin/env python3
"""调用格式网关端点：OpenAI / Claude / Gemini / TTS / STT / Embeddings。

模型名原样透传上游（调用格式与模型名无关）。
路由通过 request.app.state 访问注入的依赖（settings/store/upstream/panel_auth）。
"""

import base64
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from core.converters import (
    claude_to_openai_request,
    openai_to_claude_response,
    ClaudeStreamTransformer,
    gemini_to_openai_request,
    openai_to_gemini_response,
    GeminiStreamTransformer,
)
from .upstream import NoKeyAvailable, UpstreamError, sse_objects

ROUTER = APIRouter()

STREAM_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


# ---- 错误响应（三种格式各自的错误结构） ----

def openai_error(message, status=400, err_type="invalid_request_error", code=None):
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": err_type, "code": code}},
    )


def claude_error(message, status=400, err_type="invalid_request_error"):
    return JSONResponse(
        status_code=status,
        content={"type": "error", "error": {"type": err_type, "message": message}},
    )


def gemini_error(message, status=400):
    return JSONResponse(
        status_code=status,
        content={"error": {"code": status, "message": message,
                           "status": "INVALID_ARGUMENT"}},
    )


def _upstream_error_response(exc: UpstreamError, style: str):
    if style == "claude":
        return claude_error(str(exc), status=exc.status_code, err_type="api_error")
    if style == "gemini":
        return gemini_error(str(exc), status=exc.status_code)
    return openai_error(str(exc), status=exc.status_code, err_type="upstream_error")


def _no_key_response(style: str):
    if style == "claude":
        return claude_error("Key 池为空或没有可用 Key", status=503, err_type="api_error")
    if style == "gemini":
        return gemini_error("Key 池为空或没有可用 Key", status=503)
    return openai_error("Key 池为空或没有可用 Key", status=503, err_type="pool_empty")


# ---- 模型列表（三种格式共用；自动获取上游真实模型 + TTL 缓存） ----

MODELS_CACHE_TTL = 300  # 模型列表缓存 5 分钟，避免每次请求都打上游


async def _collect_models(app):
    """合并配置置顶模型与上游真实模型列表，带 TTL 缓存。

    - 上游正常：配置的 models_list 置顶 + 上游自动发现的模型（去重合并），
      结果缓存 5 分钟；上游新增模型最迟 5 分钟自动出现
    - 上游异常/池子为空：返回上次缓存（比配置兜底更强），从未成功过则只剩配置
    - models_list 完全可以留空（[]），即列表完全来自上游
    """
    import time

    cache = getattr(app.state, "models_cache", None)
    now = time.monotonic()
    if cache and now - cache[1] < MODELS_CACHE_TTL:
        return list(cache[0])

    models = list(app.state.settings.models_list or [app.state.settings.default_model])
    try:
        response = await app.state.upstream.call("GET", "/v1/models")
        if response.status_code == 200:
            seen = set(models)
            for item in response.json().get("data") or []:
                model_id = item.get("id") if isinstance(item, dict) else item
                if model_id and model_id not in seen:
                    models.append(model_id)
            app.state.models_cache = (list(models), now)
    except (NoKeyAvailable, UpstreamError):
        # 上游不可用：维持配置兜底（缓存若存在上面已经返回了）
        pass
    return models


@ROUTER.get("/v1/models")
async def openai_models(request: Request):
    """OpenAI 格式模型列表；带 x-api-key（Claude SDK）时返回 Anthropic 结构。"""
    models = await _collect_models(request.app)
    if request.headers.get("x-api-key") and not request.headers.get("authorization"):
        return {
            "data": [
                {"type": "model", "id": m, "display_name": m, "created_at": None}
                for m in models
            ],
            "first_id": models[0] if models else None,
            "last_id": models[-1] if models else None,
            "has_more": False,
        }
    return {
        "object": "list",
        "data": [{"id": m, "object": "model", "owned_by": "mistral"} for m in models],
    }


@ROUTER.get("/v1beta/models")
async def gemini_models(request: Request):
    models = await _collect_models(request.app)
    return {
        "models": [
            {
                "name": f"models/{m}",
                "displayName": m,
                "supportedGenerationMethods": ["generateContent", "streamGenerateContent"],
            }
            for m in models
        ]
    }


# ---- OpenAI 格式 ----

@ROUTER.post("/v1/chat/completions")
async def openai_chat_completions(request: Request):
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return openai_error("请求体不是合法 JSON")
    if not str(body.get("model") or "").strip():
        return openai_error("缺少 model 字段")
    stream = bool(body.get("stream"))

    try:
        if not stream:
            response = await request.app.state.upstream.call(
                "POST", "/v1/chat/completions", json_body=body)
            if response.status_code != 200:
                return openai_error(
                    f"上游返回 {response.status_code}: {response.text[:500]}",
                    status=response.status_code, err_type="upstream_error")
            return JSONResponse(response.json())

        entry, response = await request.app.state.upstream.open_stream(
            "POST", "/v1/chat/completions", json_body=body)
    except NoKeyAvailable:
        return _no_key_response("openai")
    except UpstreamError as exc:
        return _upstream_error_response(exc, "openai")

    async def passthrough():
        try:
            async for chunk in response.aiter_bytes():
                yield chunk
        finally:
            await response.aclose()
            request.app.state.store.mark_result(entry, ok=True)

    return StreamingResponse(passthrough(), media_type="text/event-stream")


@ROUTER.post("/v1/completions")
async def openai_completions(request: Request):
    body = await request.json()
    if not str(body.get("model") or "").strip():
        return openai_error("缺少 model 字段")
    stream = bool(body.get("stream"))
    try:
        entry, response = await request.app.state.upstream.open_stream(
            "POST", "/v1/completions", json_body=body)
    except NoKeyAvailable:
        return _no_key_response("openai")
    except UpstreamError as exc:
        return _upstream_error_response(exc, "openai")

    async def passthrough():
        try:
            async for chunk in response.aiter_bytes():
                yield chunk
        finally:
            await response.aclose()
            request.app.state.store.mark_result(entry, ok=True)

    return StreamingResponse(
        passthrough(),
        media_type="text/event-stream" if stream else "application/json",
    )


@ROUTER.post("/v1/embeddings")
async def openai_embeddings(request: Request):
    body = await request.json()
    if not str(body.get("model") or "").strip():
        body["model"] = "mistral-embed"
    try:
        response = await request.app.state.upstream.call(
            "POST", "/v1/embeddings", json_body=body)
    except NoKeyAvailable:
        return _no_key_response("openai")
    except UpstreamError as exc:
        return _upstream_error_response(exc, "openai")
    if response.status_code != 200:
        return openai_error(
            f"上游返回 {response.status_code}: {response.text[:500]}",
            status=response.status_code, err_type="upstream_error")
    return JSONResponse(response.json())


# ---- Claude 格式 ----

@ROUTER.post("/v1/messages")
async def claude_messages(request: Request):
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return claude_error("请求体不是合法 JSON")
    if not str(body.get("model") or "").strip():
        return claude_error("缺少 model 字段")
    upstream_body = claude_to_openai_request(body)
    upstream_body["model"] = body["model"]  # 原样透传
    upstream_body["stream"] = bool(body.get("stream"))

    try:
        if not upstream_body["stream"]:
            response = await request.app.state.upstream.call(
                "POST", "/v1/chat/completions", json_body=upstream_body)
            if response.status_code != 200:
                return claude_error(
                    f"上游返回 {response.status_code}: {response.text[:500]}",
                    status=response.status_code, err_type="api_error")
            return JSONResponse(
                openai_to_claude_response(response.json(), body["model"]))

        entry, response = await request.app.state.upstream.open_stream(
            "POST", "/v1/chat/completions", json_body=upstream_body)
    except NoKeyAvailable:
        return _no_key_response("claude")
    except UpstreamError as exc:
        return _upstream_error_response(exc, "claude")

    transformer = ClaudeStreamTransformer(body["model"])

    async def claude_stream():
        try:
            async for chunk_dict in sse_objects(response):
                events = transformer.feed(chunk_dict)
                if events:
                    yield events
            yield transformer.finish()
        finally:
            await response.aclose()
            request.app.state.store.mark_result(entry, ok=True)

    return StreamingResponse(
        claude_stream(), media_type="text/event-stream", headers=STREAM_HEADERS)


@ROUTER.post("/v1/messages/count_tokens")
async def claude_count_tokens(request: Request):
    body = await request.json()
    total = len(json.dumps(body.get("messages") or [], ensure_ascii=False)) // 4 + 16
    return {"input_tokens": total}


# ---- Gemini 格式 ----

async def _gemini_core(request: Request, model: str, action: str):
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return gemini_error("请求体不是合法 JSON")
    upstream_body = gemini_to_openai_request(body)
    upstream_body["model"] = model or request.app.state.settings.default_model

    stream = action == "streamGenerateContent"
    upstream_body["stream"] = stream
    try:
        if not stream:
            response = await request.app.state.upstream.call(
                "POST", "/v1/chat/completions", json_body=upstream_body)
            if response.status_code != 200:
                return gemini_error(
                    f"上游返回 {response.status_code}: {response.text[:500]}",
                    status=response.status_code)
            return JSONResponse(openai_to_gemini_response(response.json()))

        entry, response = await request.app.state.upstream.open_stream(
            "POST", "/v1/chat/completions", json_body=upstream_body)
    except NoKeyAvailable:
        return _no_key_response("gemini")
    except UpstreamError as exc:
        return _upstream_error_response(exc, "gemini")

    transformer = GeminiStreamTransformer()

    async def gemini_stream():
        try:
            async for chunk_dict in sse_objects(response):
                events = transformer.feed(chunk_dict)
                if events:
                    yield events
            yield transformer.finish()
        finally:
            await response.aclose()
            request.app.state.store.mark_result(entry, ok=True)

    return StreamingResponse(
        gemini_stream(), media_type="text/event-stream", headers=STREAM_HEADERS)


@ROUTER.post("/v1beta/models/{model_action}")
async def gemini_action(request: Request, model_action: str):
    """统一入口：models/<model>:generateContent / :streamGenerateContent。"""
    if ":" not in model_action:
        return gemini_error(
            f"路径缺少 :action（generateContent / streamGenerateContent）: {model_action}")
    model, _, action = model_action.partition(":")
    model = model.removeprefix("models/")
    if action not in ("generateContent", "streamGenerateContent"):
        return gemini_error(f"不支持的 action: {action}")
    return await _gemini_core(request, model, action)


# ---- TTS / STT ----

@ROUTER.post("/v1/audio/speech")
async def audio_speech(request: Request):
    """TTS：OpenAI speech 兼容入口，适配 Mistral /v1/audio/speech。

    实测上游行为：
      - 模型用 voxtral-mini-tts-latest（缺省自动补）
      - voice 必填且要传音色 slug（如 en_paul_neutral，完整列表
        GET /v1/audio/voices）；缺省自动补 en_paul_neutral
      - 响应是 JSON {"audio_data": "<base64 mp3>"}，这里解码成原始 MP3
        以 audio/mpeg 返回，与 OpenAI speech 客户端兼容
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return openai_error("请求体不是合法 JSON")
    if not str(body.get("model") or "").strip():
        body["model"] = "voxtral-mini-tts-latest"
    if not str(body.get("voice") or "").strip():
        body["voice"] = "en_paul_neutral"

    try:
        entry, response = await request.app.state.upstream.open_stream(
            "POST", "/v1/audio/speech", json_body=body)
    except NoKeyAvailable:
        return _no_key_response("openai")
    except UpstreamError as exc:
        return _upstream_error_response(exc, "openai")

    upstream_type = response.headers.get("content-type", "")
    # 音频直出（兼容上游未来行为）
    if "audio" in upstream_type:
        async def audio_stream():
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            finally:
                await response.aclose()
                request.app.state.store.mark_result(entry, ok=True)
        return StreamingResponse(audio_stream(), media_type=upstream_type)

    # JSON {"audio_data": base64} -> 解码为 MP3 返回
    payload = await response.aread()
    await response.aclose()
    request.app.state.store.mark_result(entry, ok=True)
    if response.status_code != 200:
        return openai_error(
            f"上游返回 {response.status_code}: {response.text[:500]}",
            status=response.status_code, err_type="upstream_error")
    try:
        audio_bytes = base64.b64decode(response.json().get("audio_data") or "")
    except (json.JSONDecodeError, ValueError, TypeError):
        return openai_error("上游 TTS 响应中缺少 audio_data")
    if not audio_bytes:
        return openai_error("上游 TTS 响应 audio_data 为空", err_type="upstream_error")
    return Response(content=audio_bytes, media_type="audio/mpeg")


@ROUTER.get("/v1/audio/voices")
async def audio_voices(request: Request):
    """列出上游全部可用音色（透传 GET /v1/audio/voices）。"""
    try:
        response = await request.app.state.upstream.call("GET", "/v1/audio/voices")
    except NoKeyAvailable:
        return _no_key_response("openai")
    except UpstreamError as exc:
        return _upstream_error_response(exc, "openai")
    if response.status_code != 200:
        return openai_error(
            f"上游返回 {response.status_code}: {response.text[:300]}",
            status=response.status_code, err_type="upstream_error")
    return JSONResponse(response.json())


@ROUTER.post("/v1/audio/transcriptions")
async def audio_transcriptions(request: Request):
    """STT：multipart 原样透传 Mistral /v1/audio/transcriptions（Voxtral）。"""
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" not in content_type:
        return openai_error("需要 multipart/form-data 上传音频文件")
    raw = await request.body()
    if not raw:
        return openai_error("请求体为空")
    try:
        entry, response = await request.app.state.upstream.open_stream(
            "POST", "/v1/audio/transcriptions",
            content=raw, headers={"Content-Type": content_type})
    except NoKeyAvailable:
        return _no_key_response("openai")
    except UpstreamError as exc:
        return _upstream_error_response(exc, "openai")
    payload = await response.aread()
    await response.aclose()
    request.app.state.store.mark_result(entry, ok=True)
    return JSONResponse(
        json.loads(payload) if payload[:1] in (b"{", b"[")
        else {"text": payload.decode("utf-8", "replace")},
        status_code=response.status_code,
    )
