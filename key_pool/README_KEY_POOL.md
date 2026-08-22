# Mistral Key 号池网关（key_pool/）

批量导入 Mistral API Key 形成号池，对外提供 CPA（CLIProxyAPI）风格的多格式调用端点：
OpenAI / Claude / Gemini / TTS / STT / Embeddings，全部支持流式。上游统一转发到
`api.mistral.ai`，每次请求从池中轮询取 Key，401/403/429 自动换 Key 重试。

> 模型名原样透传：三种调用格式共用 Mistral 的模型名（`mistral-small-latest` 等，
> `GET /v1/models` 返回上游真实列表），不做任何改名/别名映射。

## 目录结构

```
key_pool/
├── run.py              # 入口：参数解析 -> create_app() -> uvicorn
├── app/                # 应用层（FastAPI）
│   ├── config.py       #   配置加载 + 启动校验（报错说人话）
│   ├── logging_setup.py#   日志：控制台 + logs/key_pool.log 滚动文件
│   ├── auth.py         #   池访问鉴权（Bearer/x-api-key/x-goog-api-key）
│   ├── panel_auth.py   #   面板密码（PBKDF2/会话 token/限流）
│   ├── upstream.py     #   上游转发 + 换 Key 重试 + 调用日志
│   ├── gateway.py      #   OpenAI/Claude/Gemini/TTS/STT/embeddings 端点
│   ├── admin_api.py    #   管理端点（统计/导入/启停/导出）
│   └── server.py       #   create_app() 应用工厂
├── core/               # 核心层（无 FastAPI 依赖，可独立测试）
│   ├── key_store.py    #   池存储：轮询/失败禁用/滚动备份/后台落盘
│   ├── key_importer.py #   4 种导入格式解析（含恶意 zip 防护）
│   └── converters.py   #   Claude/Gemini <-> OpenAI 转换（含流式事件）
├── static/             # 管理面板前端（独立文件，改前端不用动 Python）
│   ├── admin.html
│   └── admin.js
├── tests/              # pytest 套件（111 个用例，含 mock 上游）
├── Dockerfile / docker-compose.yml
├── requirements.txt / requirements-dev.txt
└── run_key_pool.bat / run_tests.bat
```

## 快速开始

```bat
cd key_pool
run_key_pool.bat          # 或 python run.py
run_tests.bat             # 跑测试
python run.py --dev       # 开发模式：代码改动自动重载
python run.py --port 9000 # 覆盖端口
```

默认监听 `http://127.0.0.1:8787`。配置 `key_pool_config.json`：

| 配置项 | 说明 |
|---|---|
| `pool_api_keys` | 调用方访问本服务的密钥（空数组 = 不鉴权，仅本地用） |
| `admin_key` | 管理 API 的密钥（curl/脚本用；网页面板用独立登录密码） |
| `upstream_proxy` | 出站代理（国内环境 `api.mistral.ai` 被墙时必填）；留空直连 |
| `key_retry_on_rate_limit` | 429/401 时换 Key 重试次数 |
| `data_file` | 池持久化文件（pool_data.json） |

> 真实密钥写在 `key_pool_config.local.json`（同目录，会覆盖主配置，已被 git 忽略）。

启动时配置有误会直接说人话（`配置项 port 必须是数字` 而不是堆栈）；
端口被占会给出排查命令（netstat/taskkill）后干净退出。

## 面板密码（管理界面登录）

管理界面 `http://127.0.0.1:8787/` 打开时：

- **首次进入**：弹出"初始化面板"，设置一个至少 6 位的密码（PBKDF2 哈希存
  `panel_auth.json`，已被 git 忽略）
- **之后进入**：输入密码登录，会话 token 有效 12 小时；连错 5 次锁 1 分钟
- 密码只用于网页面板；脚本调用管理 API 仍用 `admin_key`（`X-Admin-Key` 头）
- **修改密码（服务运行中可用，立即生效，无需重启）**：
  ```bash
  python set_panel_password.py            # 交互式输入（密码不进命令行历史）
  python set_panel_password.py <新密码>   # 直接指定
  ```
  手动改文件同理：`panel_auth.json` 的 `hash` = PBKDF2-SHA256(密码, `salt`, 200000 轮) 的 hex
- 忘记密码：停服务删 `panel_auth.json`，重启恢复"首次设置"状态

前端是独立文件（`static/admin.html` + `admin.js`）——改前端刷新页面即可，
不用重启服务，也不会再碰 Python 代码。

## 批量导入（4 种格式）

管理界面打开后：

1. **单个 TXT 导入** —— 拖入 / 选择 `.txt`，或路径导入 `D:\keys\all.txt`
2. **ZIP 平铺 TXT** —— 拖入 `batch.zip`，包内直接是 `a.txt`、`b.txt`…
3. **ZIP 内文件夹嵌套 TXT** —— 任意深度递归收集
4. **直接文件夹导入** —— 路径填 `D:\keys\mistral_keys`

每行内容自动识别，取 32 位字母数字字段为 Key：

```
Ab3xYz...9QmK                              # 纯 Key
u@792792.xyz----密码----Ab3xYz...9QmK     # 邮箱----密码----Key
Ab3xYz...9QmK----u@792792.xyz             # Key----邮箱
u@792792.xyz:Ab3xYz...9QmK                # 邮箱:Key
```

`mistral_keys/<邮箱>.txt` 单 Key 文件自动把邮箱记为备注。安全约束：单文件
200MB、解压总量 500MB 上限；损坏/伪造 zip 返回 400；路径穿越名会被清洗。

导入 API（脚本用）：

```bash
curl -X POST http://127.0.0.1:8787/admin/import/upload \
  -H "X-Admin-Key: <admin_key>" -F "file=@batch.zip"

curl -X POST http://127.0.0.1:8787/admin/import/path \
  -H "X-Admin-Key: <admin_key>" -H "Content-Type: application/json" \
  -d '{"path": "D:/keys/mistral_keys"}'
```

## 调用格式（CPA 兼容）

调用密钥走 `pool_api_keys`，三种鉴权头都认：`Authorization: Bearer`、
`x-api-key`（Claude SDK）、`x-goog-api-key`（Gemini SDK，还支持 `?key=`）。

```python
# OpenAI SDK
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8787/v1", api_key="sk-pool-...")

# Anthropic SDK
import anthropic
client = anthropic.Anthropic(base_url="http://127.0.0.1:8787", api_key="sk-pool-...")

# Google genai SDK
from google import genai
client = genai.Client(api_key="sk-pool-...", http_options={"base_url": "http://127.0.0.1:8787"})
```

```bash
# OpenAI 流式
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Authorization: Bearer sk-pool-..." -H "Content-Type: application/json" \
  -d '{"model":"mistral-small-latest","messages":[{"role":"user","content":"你好"}],"stream":true}'

# Claude（模型名仍用 Mistral 的，协议是 Claude 的）
curl http://127.0.0.1:8787/v1/messages \
  -H "x-api-key: sk-pool-..." -H "anthropic-version: 2023-06-01" \
  -H "Content-Type: application/json" \
  -d '{"model":"mistral-small-latest","max_tokens":100,"messages":[{"role":"user","content":"你好"}],"stream":true}'

# Gemini 非流式 / 流式
curl -X POST "http://127.0.0.1:8787/v1beta/models/mistral-small-latest:generateContent" \
  -H "x-goog-api-key: sk-pool-..." -H "Content-Type: application/json" \
  -d '{"contents":[{"parts":[{"text":"你好"}]}]}'

# TTS（返回 MP3；模型/音色缺省自动补 voxtral-mini-tts-latest + en_paul_neutral）
curl -X POST http://127.0.0.1:8787/v1/audio/speech \
  -H "Authorization: Bearer sk-pool-..." -H "Content-Type: application/json" \
  -d '{"input":"Hello"}' -o out.mp3

# 音色列表
curl http://127.0.0.1:8787/v1/audio/voices -H "Authorization: Bearer sk-pool-..."

# STT（multipart 透传）/ Embeddings（mistral-embed，1024 维）
curl -X POST http://127.0.0.1:8787/v1/audio/transcriptions \
  -H "Authorization: Bearer sk-pool-..." -F file=@audio.mp3
```

## 数据安全与日志

- **滚动备份**：每次落盘前备份 `pool_data.json.bak1~bak3`（10 分钟节流）；
  主文件损坏时自动从最近备份恢复
- **防写放大**：调用统计（use_count/last_used）只标脏，后台线程每 5 秒落盘；
  池组成变化（增删/启停）仍立即写
- **日志**：`logs/key_pool.log`（10MB×5 滚动），记录每次请求的方法/路径/状态/
  耗时、每个 key 的上游调用与换 Key 重试、面板登录事件
- **调度**：轮询分配；401/403 计失败，连续 3 次自动标记失效；429 只换 Key 不计数

## 测试

```bash
cd key_pool
python -m pytest tests/          # 或 run_tests.bat
```

111 个用例覆盖：4 种导入格式、恶意 zip 防护（穿越/炸弹/损坏/exe）、KeyStore
（去重/轮询/失败禁用/备份恢复/防写放大）、三格式转换（含流式事件序列断言）、
API 集成（mock 上游：鉴权/三格式/TTS/导入/面板密码全流程/暴力锁定）、配置校验。

## 服务器部署

### 直接跑（Linux systemd）

```bash
pip install -r requirements.txt
# 配置：key_pool_config.json 里 host 改 "0.0.0.0"，密钥改成强随机值
python -c "import secrets; print('sk-pool-' + secrets.token_hex(16))"

sudo tee /etc/systemd/system/key-pool.service <<'EOF'
[Unit]
Description=Mistral Key Pool Gateway
After=network-online.target

[Service]
WorkingDirectory=/opt/key_pool
ExecStart=/usr/bin/python3 /opt/key_pool/run.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl enable --now key-pool
```

### Docker

```bash
cd key_pool
docker compose up -d --build
docker compose logs -f
```

部署要点：
- **网络**：海外机器直连即可；国内机器配 `upstream_proxy`（Docker 里用
  `http://host.docker.internal:7897`）
- **密钥必改**：公网部署 `pool_api_keys` / `admin_key` 必须换强随机值
- **HTTPS**：前面挂 nginx/caddy 做 TLS，别裸 HTTP 暴露公网
- **备份**：`pool_data.json` + `panel_auth.json` 就是全部状态，定期备份即可

## 与注册脚本联动

`mistral_key_batch.py` 产出的 `mistral_keys/` 文件夹直接用"文件夹导入"一键入池；
后续新注册的账号再次导入同一文件夹即可增量入池（重复 Key 自动跳过）。
