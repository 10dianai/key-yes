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

**密码就是配置文件里的一行明文**，简单直接：

```json
"panel_password": "admin123"
```

- **默认密码 `admin123`**：开箱即用。首次用它登录会**强制要求改成自己的密码**
  （网页上直接改，改完才进面板）
- **改密码 = 改配置**：配置里写明文（不用哈希！），重启后自动生效（服务会把它
  转成哈希存储）。想换密码就改这一行然后重启
- **网页里改密码**：登录后按流程修改，改完配置里那行会**自动清空**——之后
  重启不会被配置覆盖（配置里有值才以配置为准，空则用网页改的密码）
- 会话 token 有效 12 小时；连错 5 次锁 1 分钟
- 密码只用于网页面板；脚本调用管理 API 仍用 `admin_key`（`X-Admin-Key` 头）
- 忘记密码：改配置写一个新明文密码，重启即重置
- 命令行改法（服务运行中可用，立即生效）：`python set_panel_password.py <新密码>`

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

> 推荐 Docker 方式（最省事）；不想用 Docker 的看文末 systemd 方式。
> 部署完成后：浏览器打开 `http://服务器IP:8787/` → 用默认密码 `admin123` 登录
> → 系统强制要求你改成自己的密码 → 进入面板。

### 方式一：Docker 部署（推荐）

**第 1 步：建目录、放配置**

```bash
mkdir -p /opt/key-pool/data && cd /opt/key-pool
```

创建 `docker-compose.yml`（内容就这些，直接复制）：

```yaml
services:
  key-pool:
    image: ghcr.io/10dianai/key-yes:main
    container_name: mistral-key-pool
    ports:
      - "8787:8787"
    volumes:
      - ./data:/data
    restart: unless-stopped
```

**第 2 步：启动**

```bash
docker compose up -d
docker compose logs -f        # 看到"启动"横幅即成功，Ctrl+C 退出日志
```

首次启动会自动在 `./data/key_pool_config.json` 生成默认配置（直连 Mistral、
面板默认密码 `admin123`）。**全部状态都在 `./data` 目录里**——升级、重建容器
都不丢数据。

**第 3 步：云控制台放行端口**

阿里云/腾讯云等：控制台 → 安全组 → 入方向规则 → 放行 **TCP 8787**。
（服务器本机防火墙：`ufw allow 8787` 或 `firewall-cmd --add-port=8787/tcp --permanent && firewall-cmd --reload`）

**第 4 步：验证**

```bash
curl http://127.0.0.1:8787/healthz
# 期望输出 {"status":"ok","keys":{...}}
```

浏览器打开 `http://服务器IP:8787/` → `admin123` 登录 → 强制改密 → 进面板导入 Key。

### 日常操作

| 操作 | 命令 |
|---|---|
| 查看日志 | `docker compose logs -f` |
| 重启 | `docker restart mistral-key-pool` |
| 升级版本 | `docker compose pull && docker compose up -d` |
| 改面板密码 | 编辑 `./data/key_pool_config.json` 的 `panel_password` 写明文 → `docker restart mistral-key-pool` |
| 改 API 密钥 | 同上，编辑 `pool_api_keys` / `admin_key` → 重启 |
| 备份 | 备份整个 `./data` 目录 |
| 换机器迁移 | 拷贝 `./data` 目录 + 同样三步部署 |

### 国内服务器必须配代理（海外服务器跳过）

国内机器直连不了 `api.mistral.ai`，症状：面板能开但**调用全部失败**。修复：

```bash
# 编辑 ./data/key_pool_config.json，加一行（改成你实际的代理地址）：
"upstream_proxy": "http://host.docker.internal:7897"

# 代理跑在宿主机上时用 host.docker.internal；跑在其他机器上写它的 IP
docker restart mistral-key-pool
```

### 公网安全清单（对外服务必做）

1. `pool_api_keys` 和 `admin_key` 换成强随机值：
   `python3 -c "import secrets; print('sk-pool-' + secrets.token_hex(16))"`
2. 前置 nginx/caddy 做 HTTPS（裸 HTTP 公网传输 Key 等于裸奔）
3. 面板密码改掉默认值（首次登录会强制改，已覆盖）

### 故障排查

| 症状 | 原因与修复 |
|---|---|
| 浏览器打不开面板 | 安全组没放行 8787（最常见）；或 `docker ps` 看容器是否在跑 |
| 面板能开、调用报错/超时 | 国内机器没配代理 → 见上节"国内服务器必须配代理" |
| 面板登录说"失败次数过多" | 密码连错 5 次锁 60 秒，等一分钟再输 |
| 忘记面板密码 | `./data/key_pool_config.json` 里 `panel_password` 写个新明文 → 重启容器 |
| 启动报"数据文件是一个文件夹" | 旧的文件级挂载残留 → `rm -rf data/pool_data.json && touch data/pool_data.json`（数据会丢，新部署无此问题） |
| 容器反复重启 | `docker compose logs` 看报错；配置 JSON 写错会有中文提示哪一项不合法 |

### 方式二：直接跑（Linux systemd，不用 Docker）

```bash
# 1. 装依赖
git clone https://github.com/10dianai/key-yes.git /opt/key_pool
cd /opt/key_pool/key_pool
pip3 install -r requirements.txt

# 2. 首次运行生成默认配置（host 改 0.0.0.0 才能外部访问）
python3 run.py   # 自动生成 key_pool_config.json 后 Ctrl+C
sed -i 's/"host": "127.0.0.1"/"host": "0.0.0.0"/' key_pool_config.json

# 3. 注册 systemd 常驻
sudo tee /etc/systemd/system/key-pool.service <<'EOF'
[Unit]
Description=Mistral Key Pool Gateway
After=network-online.target

[Service]
WorkingDirectory=/opt/key_pool/key_pool
ExecStart=/usr/bin/python3 /opt/key_pool/key_pool/run.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl enable --now key-pool
sudo systemctl status key-pool   # 看到 active (running) 即成功
```

数据文件与配置都在 `/opt/key_pool/key_pool/` 目录下（`pool_data.json`、
`panel_auth.json`、`key_pool_config.json`、`logs/`），备份这个目录即可。

## 与注册脚本联动

`mistral_key_batch.py` 产出的 `mistral_keys/` 文件夹直接用"文件夹导入"一键入池；
后续新注册的账号再次导入同一文件夹即可增量入池（重复 Key 自动跳过）。
