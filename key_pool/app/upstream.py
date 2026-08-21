#!/usr/bin/env python3
"""上游转发：带换 Key 重试、SSE 解析、调用日志。

401/403 计失败（连续 3 次自动禁用，由 KeyStore.mark_result 处理），
429 只换 Key 重试不计数。所有调用记录结构化日志。
"""

import json
import logging
import time
from typing import AsyncIterator

import httpx

from core.key_store import KeyStore

logger = logging.getLogger("keypool.upstream")


class NoKeyAvailable(Exception):
    """池子里没有可用 Key。"""


class UpstreamError(Exception):
    """上游重试用尽后仍失败（携带最后一次状态码）。"""

    def __init__(self, status_code: int, detail: str):
        super().__init__(f"上游返回 {status_code}: {detail[:300]}")
        self.status_code = status_code
        self.detail = detail


class UpstreamClient:
    """对 api.mistral.ai 的转发客户端（依赖注入 store 与 settings）。"""

    def __init__(self, settings, store: KeyStore, client: httpx.AsyncClient):
        self.settings = settings
        self.store = store
        self.client = client

    # ---- 鉴权头 ----

    def _pick_with_log(self, attempt: int):
        entry = self.store.pick()
        if entry is None:
            raise NoKeyAvailable("Key 池为空或没有可用 Key，请先导入")
        if attempt > 0:
            logger.info("换 Key 重试 第%d次 -> %s", attempt, entry["id"])
        return entry

    @staticmethod
    def _headers_for(entry) -> dict:
        return {"Authorization": f"Bearer {entry['key']}"}

    # ---- 非流式 ----

    async def call(self, method, path, *, json_body=None, content=None, headers=None):
        """非流式调用上游。返回 httpx.Response。

        401/403/429/网络错误时换 Key 重试（次数由配置决定），
        全部失败后抛 UpstreamError 或 NoKeyAvailable。
        """
        retries = int(self.settings.key_retry_on_rate_limit)
        last_response = None
        for attempt in range(retries + 1):
            entry = self._pick_with_log(attempt)
            request_headers = self._headers_for(entry)
            if headers:
                request_headers.update(headers)
            started = time.monotonic()
            try:
                response = await self.client.request(
                    method,
                    self.settings.upstream_base_url + path,
                    json=json_body,
                    content=content,
                    headers=request_headers,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                self.store.mark_result(entry, ok=False, error=f"网络错误: {exc}")
                logger.warning("key=%s %s 网络错误: %s", entry["id"], path, exc)
                last_response = None
                continue
            elapsed = int((time.monotonic() - started) * 1000)
            if response.status_code in (401, 403):
                self.store.mark_result(entry, ok=False, error=f"上游 {response.status_code}")
                logger.warning("key=%s %s 上游%d（计失败）%dms",
                               entry["id"], path, response.status_code, elapsed)
                last_response = response
                continue
            if response.status_code == 429:
                logger.warning("key=%s %s 上游429（只换不计）%dms",
                               entry["id"], path, elapsed)
                last_response = response
                continue
            logger.info("key=%s %s -> %d %dms", entry["id"], path,
                        response.status_code, elapsed)
            self.store.mark_result(entry, ok=True)
            return response
        if last_response is not None:
            raise UpstreamError(last_response.status_code, last_response.text)
        raise NoKeyAvailable("重试次数用尽且没有可用的 Key")

    # ---- 流式 ----

    async def open_stream(self, method, path, *, json_body=None, content=None, headers=None):
        """流式调用上游。返回 (entry, response)。

        在产出任何字节给调用方之前完成状态码检查与换 Key 重试，
        调用方负责 response.aclose() 并 mark_result。
        """
        retries = int(self.settings.key_retry_on_rate_limit)
        last_error = None
        for attempt in range(retries + 1):
            entry = self._pick_with_log(attempt)
            request_headers = self._headers_for(entry)
            if headers:
                request_headers.update(headers)
            request = self.client.build_request(
                method,
                self.settings.upstream_base_url + path,
                json=json_body,
                content=content,
                headers=request_headers,
            )
            started = time.monotonic()
            try:
                response = await self.client.send(request, stream=True)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                self.store.mark_result(entry, ok=False, error=f"网络错误: {exc}")
                logger.warning("key=%s %s 流式网络错误: %s", entry["id"], path, exc)
                continue
            elapsed = int((time.monotonic() - started) * 1000)
            if response.status_code in (401, 403, 429):
                await response.aread()
                if response.status_code != 429:
                    self.store.mark_result(entry, ok=False,
                                           error=f"上游 {response.status_code}")
                logger.warning("key=%s %s 流式上游%d %dms",
                               entry["id"], path, response.status_code, elapsed)
                last_error = (response.status_code, response.text)
                await response.aclose()
                continue
            logger.info("key=%s %s -> %d（流）%dms", entry["id"], path,
                        response.status_code, elapsed)
            return entry, response
        if last_error is not None:
            raise UpstreamError(last_error[0], last_error[1])
        raise NoKeyAvailable("重试次数用尽且没有可用的 Key")


async def sse_objects(response) -> AsyncIterator[dict]:
    """把上游 OpenAI 风格 SSE 流解析成 dict 迭代器。"""
    buffer = ""
    async for chunk in response.aiter_text():
        buffer += chunk
        while "\n\n" in buffer:
            block, buffer = buffer.split("\n\n", 1)
            for line in block.split("\n"):
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    yield json.loads(payload)
                except json.JSONDecodeError:
                    continue
