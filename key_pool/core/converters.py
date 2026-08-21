#!/usr/bin/env python3
"""请求/响应格式转换器：Claude、Gemini <-> OpenAI（Mistral 上游是 OpenAI 风格）。

涵盖非流式响应转换与流式 SSE 事件转换：
  - claude_to_openai_request      Claude /v1/messages 请求体  -> OpenAI chat/completions
  - openai_to_claude_response     OpenAI 非流式响应           -> Claude message
  - ClaudeStreamTransformer       OpenAI SSE chunk 流         -> Claude SSE 事件流
  - gemini_to_openai_request      Gemini generateContent 请求 -> OpenAI chat/completions
  - openai_to_gemini_response     OpenAI 非流式响应           -> Gemini candidates
  - GeminiStreamTransformer       OpenAI SSE chunk 流         -> Gemini alt=sse 事件流

工具调用（tools/tool_use/functionCall）在文本之外做最大兼容转换。
"""

import json

# ---- 通用 ----


def _content_to_text(content):
    """OpenAI content 可能是 str 或 [{type:text,...}] 分块列表。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text") or "")
        return "\n".join(p for p in parts if p)
    return ""


# ---- Claude -> OpenAI ----

CLAUDE_STOP_REASON = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "end_turn",
}


def claude_to_openai_request(body):
    messages = []

    system = body.get("system")
    if isinstance(system, str) and system.strip():
        messages.append({"role": "system", "content": system})
    elif isinstance(system, list):
        text = "\n".join(
            block.get("text", "")
            for block in system
            if isinstance(block, dict) and block.get("type") == "text"
        )
        if text.strip():
            messages.append({"role": "system", "content": text})

    for msg in body.get("messages") or []:
        role = msg.get("role", "user")
        content = msg.get("content")

        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            messages.append({"role": role, "content": _content_to_text(content)})
            continue

        texts, images, tool_calls, tool_results = [], [], [], []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                texts.append(block.get("text") or "")
            elif btype == "image":
                source = block.get("source") or {}
                if source.get("type") == "base64":
                    images.append({
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{source.get('media_type', 'image/png')};base64,{source.get('data', '')}"
                        },
                    })
            elif btype == "tool_use":
                tool_calls.append({
                    "id": block.get("id") or f"call_{len(tool_calls)}",
                    "type": "function",
                    "function": {
                        "name": block.get("name") or "",
                        "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                    },
                })
            elif btype == "tool_result":
                inner = block.get("content")
                if isinstance(inner, list):
                    text = "\n".join(
                        b.get("text", "") for b in inner if isinstance(b, dict)
                    )
                else:
                    text = str(inner or "")
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id") or "",
                    "content": text,
                })

        if role == "assistant":
            assistant = {"role": "assistant", "content": "\n".join(texts) or None}
            if tool_calls:
                assistant["tool_calls"] = tool_calls
            messages.append(assistant)
        else:
            if images:
                combined = [{"type": "text", "text": "\n".join(texts)}] if texts else []
                messages.append({"role": role, "content": combined + images})
            else:
                messages.append({"role": role, "content": "\n".join(texts)})
            messages.extend(tool_results)

    request = {"messages": messages}
    if body.get("max_tokens"):
        request["max_tokens"] = body["max_tokens"]
    if body.get("temperature") is not None:
        request["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        request["top_p"] = body["top_p"]
    if body.get("stop_sequences"):
        request["stop"] = body["stop_sequences"]

    tools = []
    for tool in body.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "computer_20241022" or str(tool.get("type", "")).startswith("computer_"):
            continue  # Claude 专有 computer use，OpenAI 无对应
        tools.append({
            "type": "function",
            "function": {
                "name": tool.get("name") or "",
                "description": tool.get("description") or "",
                "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
            },
        })
    if tools:
        request["tools"] = tools
    return request


def openai_to_claude_response(openai_resp, model):
    choices = openai_resp.get("choices") or [{}]
    choice = choices[0] or {}
    message = choice.get("message") or {}
    usage = openai_resp.get("usage") or {}

    content = []
    if message.get("content"):
        content.append({"type": "text", "text": message["content"]})
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except json.JSONDecodeError:
            arguments = {"raw": function.get("arguments") or ""}
        content.append({
            "type": "tool_use",
            "id": call.get("id") or "toolu_unknown",
            "name": function.get("name") or "",
            "input": arguments,
        })

    return {
        "id": f"msg_{openai_resp.get('id') or 'unknown'}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content or [{"type": "text", "text": ""}],
        "stop_reason": CLAUDE_STOP_REASON.get(choice.get("finish_reason"), "end_turn"),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


class ClaudeStreamTransformer:
    """把上游 OpenAI SSE chunk 流实时转换为 Claude SSE 事件流。

    用法：
        transformer = ClaudeStreamTransformer(model)
        async for chunk in upstream_sse_bytes:
            for event_line in transformer.feed(chunk): ...
        for event_line in transformer.finish(): ...
    """

    def __init__(self, model):
        self.model = model
        self._started = False
        self._open_index = -1         # 当前打开的 content block 序号，-1 表示没有
        self._next_index = 0          # 已分配的最大序号 + 1（block 序号只增不减）
        self._block_kind = None       # "text" | "tool"
        self._tool_buffer = {}        # index -> {id, name, args_json, block}
        self._finish_reason = None
        self._usage = {}

    def _sse(self, event, data):
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    def _emit_start(self):
        """首个 chunk 到达时发 message_start + 首个 text block。"""
        self._started = True
        self._open_index = self._next_index
        self._next_index += 1
        self._block_kind = "text"
        message = {
            "id": "msg_stream",
            "type": "message",
            "role": "assistant",
            "model": self.model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
        out = self._sse("message_start", {"type": "message_start", "message": message})
        out += self._sse("content_block_start", {
            "type": "content_block_start",
            "index": self._open_index,
            "content_block": {"type": "text", "text": ""},
        })
        return out

    def _close_block(self):
        if self._open_index < 0:
            return ""
        out = self._sse("content_block_stop", {
            "type": "content_block_stop", "index": self._open_index
        })
        self._open_index = -1
        self._block_kind = None
        return out

    def feed(self, chunk_dict):
        """吃一个 OpenAI chunk dict，返回要下发的事件文本。"""
        if not self._started:
            # message_start 提前在 finish 时也能补发
            if chunk_dict.get("usage"):
                self._usage = chunk_dict["usage"] or {}
            out = self._emit_start()
        else:
            out = ""

        choices = chunk_dict.get("choices") or []
        if not choices:
            if chunk_dict.get("usage"):
                self._usage = chunk_dict["usage"] or {}
            return out

        choice = choices[0] or {}
        delta = choice.get("delta") or {}
        finish_reason = choice.get("finish_reason")
        if finish_reason:
            self._finish_reason = finish_reason

        # 文本增量
        text = delta.get("content")
        if isinstance(text, list):
            text = _content_to_text(text)
        if text:
            if self._block_kind != "text":
                out += self._close_block()
                self._open_index = self._next_index
                self._next_index += 1
                self._block_kind = "text"
                out += self._sse("content_block_start", {
                    "type": "content_block_start",
                    "index": self._open_index,
                    "content_block": {"type": "text", "text": ""},
                })
            out += self._sse("content_block_delta", {
                "type": "content_block_delta",
                "index": self._open_index,
                "delta": {"type": "text_delta", "text": text},
            })

        # 工具调用增量（OpenAI 按 index 分流）
        for call in delta.get("tool_calls") or []:
            index = int(call.get("index") or 0)
            function = call.get("function") or {}
            buffer = self._tool_buffer.setdefault(index, {
                "id": call.get("id") or f"toolu_{index}",
                "name": "",
                "args": "",
                "block": None,
            })
            if call.get("id"):
                buffer["id"] = call["id"]
            if function.get("name"):
                buffer["name"] += function["name"]
            args_delta = function.get("arguments")
            if args_delta:
                buffer["args"] += args_delta
            if buffer["block"] is None and buffer["name"]:
                out += self._close_block()
                self._open_index = self._next_index
                self._next_index += 1
                buffer["block"] = self._open_index
                self._block_kind = "tool"
                out += self._sse("content_block_start", {
                    "type": "content_block_start",
                    "index": self._open_index,
                    "content_block": {
                        "type": "tool_use",
                        "id": buffer["id"],
                        "name": buffer["name"],
                        "input": {},
                    },
                })
            elif args_delta and buffer["block"] is not None:
                out += self._sse("content_block_delta", {
                    "type": "content_block_delta",
                    "index": buffer["block"],
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": args_delta,
                    },
                })
        return out

    def finish(self):
        """上游结束后收尾，返回剩余事件文本。"""
        out = ""
        if not self._started:
            out += self._emit_start()
        out += self._close_block()
        usage = self._usage or {}
        out += self._sse("message_delta", {
            "type": "message_delta",
            "delta": {
                "stop_reason": CLAUDE_STOP_REASON.get(self._finish_reason, "end_turn"),
                "stop_sequence": None,
            },
            "usage": {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
            },
        })
        out += self._sse("message_stop", {"type": "message_stop"})
        return out


# ---- Gemini -> OpenAI ----

GEMINI_FINISH = {
    "stop": "STOP",
    "length": "MAX_TOKENS",
    "tool_calls": "STOP",
    "content_filter": "SAFETY",
}


def gemini_to_openai_request(body):
    messages = []

    system = body.get("systemInstruction") or body.get("system_instruction")
    if isinstance(system, dict):
        text = "\n".join(
            part.get("text", "")
            for part in system.get("parts") or []
            if isinstance(part, dict)
        )
        if text.strip():
            messages.append({"role": "system", "content": text})
    elif isinstance(system, str) and system.strip():
        messages.append({"role": "system", "content": system})

    for item in body.get("contents") or []:
        role = "assistant" if item.get("role") == "model" else "user"
        parts = item.get("parts") or []
        texts, images = [], []
        for part in parts:
            if not isinstance(part, dict):
                continue
            if "text" in part:
                texts.append(part.get("text") or "")
            elif "inlineData" in part or "inline_data" in part:
                inline = part.get("inlineData") or part.get("inline_data") or {}
                images.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{inline.get('mimeType', 'image/png')};base64,{inline.get('data', '')}"
                    },
                })
        if images:
            combined = [{"type": "text", "text": "\n".join(texts)}] if texts else []
            messages.append({"role": role, "content": combined + images})
        elif texts:
            messages.append({"role": role, "content": "\n".join(texts)})

    request = {"messages": messages}
    gen = body.get("generationConfig") or body.get("generation_config") or {}
    if gen.get("temperature") is not None:
        request["temperature"] = gen["temperature"]
    if gen.get("maxOutputTokens") or gen.get("max_output_tokens"):
        request["max_tokens"] = gen.get("maxOutputTokens") or gen.get("max_output_tokens")
    if gen.get("topP") is not None:
        request["top_p"] = gen["topP"]
    if gen.get("topK") is not None:
        request["top_k"] = gen.get("topK") or gen.get("top_k")

    tools = []
    for tool in body.get("tools") or []:
        for declaration in (tool or {}).get("functionDeclarations") or []:
            tools.append({
                "type": "function",
                "function": {
                    "name": declaration.get("name") or "",
                    "description": declaration.get("description") or "",
                    "parameters": declaration.get("parameters")
                                  or {"type": "object", "properties": {}},
                },
            })
    if tools:
        request["tools"] = tools
    return request


def openai_to_gemini_response(openai_resp):
    choices = openai_resp.get("choices") or [{}]
    choice = choices[0] or {}
    message = choice.get("message") or {}
    usage = openai_resp.get("usage") or {}

    parts = []
    if message.get("content"):
        parts.append({"text": message["content"]})
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except json.JSONDecodeError:
            arguments = {"raw": function.get("arguments") or ""}
        parts.append({"functionCall": {"name": function.get("name") or "", "args": arguments}})

    candidate = {
        "content": {"parts": parts or [{"text": ""}], "role": "model"},
        "finishReason": GEMINI_FINISH.get(choice.get("finish_reason"), "STOP"),
        "index": 0,
    }
    return {
        "candidates": [candidate],
        "usageMetadata": {
            "promptTokenCount": usage.get("prompt_tokens", 0),
            "candidatesTokenCount": usage.get("completion_tokens", 0),
            "totalTokenCount": usage.get("total_tokens", 0),
        },
        "modelVersion": openai_resp.get("model") or "",
    }


class GeminiStreamTransformer:
    """把上游 OpenAI SSE chunk 流转换为 Gemini streamGenerateContent?alt=sse 流。"""

    def __init__(self):
        self._finish_reason = None
        self._usage = {}

    def _sse(self, data):
        return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    def feed(self, chunk_dict):
        out = ""
        choices = chunk_dict.get("choices") or []
        if not choices:
            if chunk_dict.get("usage"):
                self._usage = chunk_dict["usage"] or {}
            return out
        choice = choices[0] or {}
        delta = choice.get("delta") or {}
        if choice.get("finish_reason"):
            self._finish_reason = choice["finish_reason"]
        if chunk_dict.get("usage"):
            self._usage = chunk_dict["usage"] or {}

        text = delta.get("content")
        if isinstance(text, list):
            text = _content_to_text(text)
        if text:
            out += self._sse({
                "candidates": [{
                    "content": {"parts": [{"text": text}], "role": "model"},
                    "index": 0,
                }],
            })

        for call in delta.get("tool_calls") or []:
            function = call.get("function") or {}
            if function.get("name") and not function.get("arguments"):
                out += self._sse({
                    "candidates": [{
                        "content": {
                            "parts": [{"functionCall": {"name": function["name"], "args": {}}}],
                            "role": "model",
                        },
                        "index": 0,
                    }],
                })
        return out

    def finish(self):
        usage = self._usage or {}
        final = {
            "candidates": [{
                "content": {"parts": [], "role": "model"},
                "finishReason": GEMINI_FINISH.get(self._finish_reason, "STOP"),
                "index": 0,
            }],
            "usageMetadata": {
                "promptTokenCount": usage.get("prompt_tokens", 0),
                "candidatesTokenCount": usage.get("completion_tokens", 0),
                "totalTokenCount": usage.get("total_tokens", 0),
            },
        }
        return self._sse(final)
