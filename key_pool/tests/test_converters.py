"""转换器单元测试：三格式请求/响应转换 + 流式事件序列。"""

import json

from core.converters import (
    claude_to_openai_request, openai_to_claude_response, ClaudeStreamTransformer,
    gemini_to_openai_request, openai_to_gemini_response, GeminiStreamTransformer,
)

CLAUDE_BODY = {
    "model": "mistral-small-latest", "max_tokens": 1024, "temperature": 0.7,
    "system": "你是助手",
    "messages": [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "好的"},
            {"type": "tool_use", "id": "tu1", "name": "get_weather",
             "input": {"city": "北京"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu1", "content": "晴"},
            {"type": "text", "text": "总结一下"},
        ]},
    ],
    "tools": [{"name": "get_weather", "description": "查天气",
               "input_schema": {"type": "object",
                                "properties": {"city": {"type": "string"}}}}],
}

OPENAI_TOOL_RESP = {
    "id": "chatcmpl-1",
    "choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": "我查一下",
        "tool_calls": [{"id": "call_1", "function": {
            "name": "get_weather", "arguments": '{"city": "北京"}'}}],
    }}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}

STREAM_CHUNKS = [
    {"id": "1", "choices": [{"delta": {"role": "assistant", "content": "你"},
                             "finish_reason": None}]},
    {"id": "1", "choices": [{"delta": {"content": "好"}, "finish_reason": None}]},
    {"id": "1", "choices": [{"delta": {"tool_calls": [
        {"index": 0, "id": "call_1",
         "function": {"name": "get_w", "arguments": ""}}]}, "finish_reason": None}]},
    {"id": "1", "choices": [{"delta": {"tool_calls": [
        {"index": 0, "function": {"arguments": '{"a"'}}]}, "finish_reason": None}]},
    {"id": "1", "choices": [{"delta": {"tool_calls": [
        {"index": 0, "function": {"arguments": ": 1}"}}]}, "finish_reason": None}]},
    {"id": "1", "choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    {"id": "1", "choices": [],
     "usage": {"prompt_tokens": 8, "completion_tokens": 6}},
]


class TestClaudeRequest:
    def test_system_and_messages(self):
        req = claude_to_openai_request(CLAUDE_BODY)
        messages = req["messages"]
        assert messages[0] == {"role": "system", "content": "你是助手"}
        assert messages[1] == {"role": "user", "content": "你好"}

    def test_tool_use_and_result(self):
        req = claude_to_openai_request(CLAUDE_BODY)
        messages = req["messages"]
        assistant = messages[2]
        assert assistant["tool_calls"][0]["function"]["name"] == "get_weather"
        assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"city": "北京"}
        tool_msg = messages[4]
        assert tool_msg["role"] == "tool" and tool_msg["tool_call_id"] == "tu1"

    def test_tools_schema(self):
        req = claude_to_openai_request(CLAUDE_BODY)
        assert req["tools"][0]["function"]["parameters"]["type"] == "object"

    def test_sampling_params(self):
        req = claude_to_openai_request(CLAUDE_BODY)
        assert req["max_tokens"] == 1024 and req["temperature"] == 0.7


class TestClaudeResponse:
    def test_tool_response(self):
        c = openai_to_claude_response(OPENAI_TOOL_RESP, "m")
        assert c["content"][0] == {"type": "text", "text": "我查一下"}
        assert c["content"][1]["type"] == "tool_use"
        assert c["content"][1]["input"] == {"city": "北京"}
        assert c["stop_reason"] == "tool_use"
        assert c["usage"] == {"input_tokens": 10, "output_tokens": 5}


class TestClaudeStream:
    def test_event_sequence(self):
        transformer = ClaudeStreamTransformer("m")
        out = ""
        for chunk in STREAM_CHUNKS:
            out += transformer.feed(chunk)
        out += transformer.finish()
        seq = [line[7:] for line in out.split("\n") if line.startswith("event: ")]
        assert seq == ["message_start", "content_block_start",
                       "content_block_delta", "content_block_delta",
                       "content_block_stop", "content_block_start",
                       "content_block_delta", "content_block_delta",
                       "content_block_stop", "message_delta", "message_stop"]

    def test_tool_json_reassembles(self):
        transformer = ClaudeStreamTransformer("m")
        out = ""
        for chunk in STREAM_CHUNKS:
            out += transformer.feed(chunk)
        out += transformer.finish()
        datas = [json.loads(line[6:]) for line in out.split("\n")
                 if line.startswith("data: ")]
        json_parts = [d["delta"]["partial_json"] for d in datas
                      if d.get("type") == "content_block_delta"
                      and d["delta"].get("type") == "input_json_delta"]
        assert json.loads("".join(json_parts)) == {"a": 1}

    def test_block_index_increments(self):
        transformer = ClaudeStreamTransformer("m")
        out = ""
        for chunk in STREAM_CHUNKS:
            out += transformer.feed(chunk)
        out += transformer.finish()
        datas = [json.loads(line[6:]) for line in out.split("\n")
                 if line.startswith("data: ")]
        starts = [d["index"] for d in datas if d.get("type") == "content_block_start"]
        assert starts == [0, 1]

    def test_empty_stream_completes(self):
        transformer = ClaudeStreamTransformer("m")
        out = transformer.finish()
        assert "message_start" in out and "message_stop" in out


class TestGeminiRequest:
    GEMINI_BODY = {
        "systemInstruction": {"parts": [{"text": "系统词"}]},
        "contents": [
            {"role": "user", "parts": [
                {"text": "画个图"},
                {"inlineData": {"mimeType": "image/png", "data": "QUJD"}}]},
            {"role": "model", "parts": [{"text": "好的"}]},
            {"role": "user", "parts": [{"text": "继续"}]},
        ],
        "generationConfig": {"temperature": 0.5, "maxOutputTokens": 512},
        "tools": [{"functionDeclarations": [
            {"name": "f", "description": "d",
             "parameters": {"type": "object", "properties": {}}}]}],
    }

    def test_conversion(self):
        req = gemini_to_openai_request(self.GEMINI_BODY)
        messages = req["messages"]
        assert messages[0] == {"role": "system", "content": "系统词"}
        assert messages[1]["content"][1]["type"] == "image_url"
        assert messages[1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
        assert messages[2] == {"role": "assistant", "content": "好的"}
        assert req["max_tokens"] == 512 and req["temperature"] == 0.5
        assert req["tools"][0]["function"]["name"] == "f"


class TestGeminiResponse:
    def test_conversion(self):
        g = openai_to_gemini_response(OPENAI_TOOL_RESP)
        assert g["candidates"][0]["content"]["parts"][0] == {"text": "我查一下"}
        assert g["candidates"][0]["content"]["parts"][1]["functionCall"]["args"] == {"city": "北京"}
        assert g["candidates"][0]["finishReason"] == "STOP"
        assert g["usageMetadata"]["promptTokenCount"] == 10


class TestGeminiStream:
    def test_text_and_final_usage(self):
        transformer = GeminiStreamTransformer()
        out = ""
        for chunk in STREAM_CHUNKS:
            out += transformer.feed(chunk)
        out += transformer.finish()
        datas = [json.loads(line[6:]) for line in out.split("\n")
                 if line.startswith("data: ")]
        texts = []
        for d in datas[:-1]:
            parts = d["candidates"][0]["content"]["parts"]
            if parts and "text" in parts[0]:
                texts.append(parts[0]["text"])
        assert "".join(texts) == "你好"
        final = datas[-1]
        assert final["candidates"][0]["finishReason"] == "STOP"
        assert final["usageMetadata"]["promptTokenCount"] == 8
