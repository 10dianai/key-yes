# key-yes — Mistral Key 号池网关

批量导入 Mistral API Key 形成号池，对外提供 OpenAI / Claude / Gemini / TTS 兼容
调用端点（CPA 风格），全部支持流式。每次请求从池中轮询取 Key，401/403/429
自动换 Key 重试，失败 Key 自动剔除。

## 功能

- **4 种批量导入**：单个 TXT、ZIP 平铺 TXT、ZIP 内文件夹嵌套 TXT、直接文件夹导入
- **三种调用协议**：OpenAI（`/v1/chat/completions`）、Claude（`/v1/messages`）、
  Gemini（`/v1beta/models/*:generateContent`）——任何官方 SDK 直连，模型名原样透传
- **语音**：TTS（`/v1/audio/speech`，返回 MP3）、STT、音色列表
- **Web 管理面板**：首次设置密码登录，拖拽导入、Key 启停/删除/导出、统计
- **工程化**：111 个 pytest 用例、滚动日志、池数据滚动备份与损坏自愈、
  Docker 一键部署

## 快速开始

```bash
cd key_pool
pip install -r requirements.txt
python run.py            # http://127.0.0.1:8787
```

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8787/v1", api_key="sk-pool-change-me-0001")
print(client.chat.completions.create(
    model="mistral-small-latest",
    messages=[{"role": "user", "content": "你好"}]).choices[0].message.content)
```

完整文档（配置/部署/Docker/安全说明）见 [key_pool/README_KEY_POOL.md](key_pool/README_KEY_POOL.md)。

## 目录

```
key_pool/
├── run.py      # 服务入口（--dev 热重载 / --port 覆盖）
├── app/        # FastAPI 应用层（鉴权/网关/管理/面板/日志）
├── core/       # 核心层：池存储、导入解析、格式转换（可独立测试）
├── static/     # 管理面板前端（HTML/JS 独立文件）
├── tests/      # pytest 套件（111 用例）
└── Dockerfile / docker-compose.yml
```

## 测试

```bash
cd key_pool && python -m pytest tests/   # 111 passed
```

## 安全提示

- 部署前必须修改 `key_pool_config.json` 里的 `pool_api_keys` / `admin_key`
- 真实密钥写 `key_pool_config.local.json`（已 gitignore）
- 公网部署建议前置 nginx/caddy 做 HTTPS
