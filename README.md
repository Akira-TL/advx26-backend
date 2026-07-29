# AdventureX Cloud Media Service

面向 SoundPola 比赛链路的单体云端服务：用户上传一段原始音频，云端通过 SQLite 作业完成音频修复与归一化、MP3 帧索引、确定性音频特征、Headless WebGL 可视化、H.264 编码和不可变媒体发布。固定 Trigger 与 Playback 使用各自的 Bearer Token 解析 NFC 内容和下载媒体。

## 运行链路

```text
User Token 上传原始音频
  → SQLite durable job
  → FFprobe / FFmpeg 音频归一化
  → audio.mp3 + audio.idx + deterministic PCM
  → 10 fps 音频特征时间线
  → Sound-Visualization-Kaleidoscope-effect 显式帧渲染
  → 480×320 / 10 fps H.264 Constrained Baseline MP4
  → 原子发布 READY manifest 与对象
  → Trigger 解析 NFC
  → Playback Range 下载 video.mp4 / audio.mp3 / audio.idx
```

一次上传生成一个不可变 `content_id`。原始音频永久保留；READY 输出不可覆盖。内容删除后设备访问立即失效，物理对象稍后清理。

## 必需环境

- Python 3.12+
- FFmpeg 与 FFprobe
- Node.js
- `Sound-Visualization-Kaleidoscope-effect/particle-field`
- Puppeteer 下载的 Chromium

本机准备渲染器：

```bash
cd ../Sound-Visualization-Kaleidoscope-effect/particle-field
npm ci
npm run build
cd ../../backend
```

安装并运行后端：

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

export BACKEND_TRIGGER_TOKEN='replace-trigger-secret'
export BACKEND_PLAYBACK_TOKEN='replace-playback-secret'
export BACKEND_WORKER_ENABLED=1
export BACKEND_RENDERER_PROJECT_DIR="$PWD/../Sound-Visualization-Kaleidoscope-effect/particle-field"

python run.py
```

交互文档位于 `/docs`，运行时 JSON 位于 `/openapi.json`，仓库内静态规范位于 `openapi/openapi.yaml`。

## 配置

| 变量 | 默认值 | 作用 |
|---|---:|---|
| `BACKEND_PUBLIC_BASE_URL` | `http://127.0.0.1:9000` | OpenAPI `servers`、外部文档和部署公开基地址 |
| `BACKEND_TRIGGER_TOKEN` | 空 | 固定 Trigger Bearer Token；readiness 要求已配置 |
| `BACKEND_PLAYBACK_TOKEN` | 空 | 固定 Playback Bearer Token；必须与 Trigger Token 不同 |
| `BACKEND_CORS_ORIGINS` | `*` | 逗号分隔的允许来源 |
| `BACKEND_WORKER_ENABLED` | `0` | `1` 时启动完整媒体 worker |
| `BACKEND_RENDERER_PROJECT_DIR` | 相邻可视化仓 | 已执行 `npm ci && npm run build` 的 renderer 目录 |
| `BACKEND_NODE_BINARY` | `node` | Node 可执行文件 |
| `BACKEND_FFMPEG_BINARY` | `ffmpeg` | FFmpeg 可执行文件 |
| `BACKEND_FFPROBE_BINARY` | `ffprobe` | FFprobe 可执行文件 |
| `BACKEND_MEDIA_COMMAND_TIMEOUT_SECONDS` | `90` | 单次 FFmpeg/FFprobe 超时 |
| `BACKEND_RENDERER_TIMEOUT_SECONDS` | `180` | 单次 Headless Chromium 渲染超时 |
| `BACKEND_MAX_AUDIO_BYTES` | `52428800` | 原始音频上限，50 MiB |
| `BACKEND_WORKER_POLL_SECONDS` | `1` | SQLite 作业轮询间隔 |
| `BACKEND_JOB_LEASE_SECONDS` | `60` | 作业租约与恢复窗口 |
| `BACKEND_PROCESSING_MAX_ATTEMPTS` | `3` | transient failure 最大处理次数 |
| `BACKEND_FAILED_STAGING_RETENTION_SECONDS` | `86400` | 失败诊断 staging 保留时间 |
| `BACKEND_FAILED_STAGING_MAX_BYTES` | `536870912` | 失败 staging 总上限 |
| `BACKEND_CHAIN_ENABLED` | `0` | `1` 时启用链上铸造（NFT mint/claim）功能 |
| `BACKEND_CHAIN_CONTRACT_ADDRESS` | 空 | ERC-721 合约地址；启用 chain 时必填 |
| `BACKEND_CHAIN_OPERATOR_PRIVATE_KEY` | 空 | 服务端代签 operator 私钥（0x + 64 hex）；用户未托管私钥时回退使用 |
| `BACKEND_CHAIN_RPC_URL` | Injective testnet | Injective EVM JSON-RPC 端点 |
| `BACKEND_CHAIN_ID` | `1439` | 链 ID（Injective testnet = 1439） |

数据默认写入：

```text
storage/
├── cloud-media.db
├── objects/
│   └── contents/{content_id}/...
└── object-staging/
    └── jobs/{job_id}/...
```

User Token 只在签发时返回一次，SQLite 仅保存摘要。Trigger/Playback Token 不由服务签发，也不应提交到 Git。

## 健康与就绪

```bash
curl http://127.0.0.1:9000/api/v1/health
curl http://127.0.0.1:9000/api/v1/ready
```

- `/health` 只表示 HTTP 进程存活。
- `/ready` 检查 SQLite、对象存储、FFmpeg、FFprobe、设备 Token；启用 worker 时还检查 renderer、Chromium 和 worker task。

## API 示例

### 1. 注册（邮箱 + 密码）

```bash
curl -sS -X POST http://127.0.0.1:9000/api/v1/users \
  -H 'Content-Type: application/json' \
  -d '{"email":"user@example.com","password":"password12345"}'
```

注册成功返回 `201`，响应头 `Cache-Control: no-store`，响应体示例：

```json
{
  "user_id": "0123456789abcdef0123456789abcdef",
  "email": "user@example.com",
  "wallet_address": "0xAbC...1234",
  "private_key": "0x<64 hex>",
  "private_key_stored": false
}
```

注册后的返回行为：

- `user_id`：账户唯一标识（32 位十六进制）。
- 密码只以 PBKDF2-HMAC-SHA256 摘要存储，绝不回显明文。
- `wallet_address`：注册时自动为用户分配的钱包地址（EIP-55 校验和格式）。该地址与账户绑定，因此钱包登录（challenge/verify）与邮箱登录等价，解析到同一 `user_id`。
- `private_key`：钱包私钥，**仅在注册响应中返回一次**。请求体可选字段 `store_private_key`（默认 `false`）决定是否由后端托管：
  - `false`（默认）：后端不存储私钥（数据库中为 `NULL`），客户端必须自行保存，丢失后无法找回。
  - `true`：后端存储私钥，`private_key_stored` 返回 `true`。
- `private_key_stored`：标识本次私钥是否被后端持久化。
- 邮箱重复注册返回 `409`。

托管私钥示例：

```bash
curl -sS -X POST http://127.0.0.1:9000/api/v1/users \
  -H 'Content-Type: application/json' \
  -d '{"email":"user@example.com","password":"password12345","store_private_key":true}'
```

### 2. 登录获取 User Token

```bash
curl -sS -X POST http://127.0.0.1:9000/api/v1/sessions \
  -H 'Content-Type: application/json' \
  -d '{"email":"user@example.com","password":"password12345"}'
```

每次登录签发一枚全新的不透明 `token`，仅返回一次，需由客户端安全保存；邮箱或密码错误返回 `401`：

```bash
export USER_TOKEN='usr_example_only'
```

### 3. 上传音频

```bash
curl -sS -X POST http://127.0.0.1:9000/api/v1/contents \
  -H "Authorization: Bearer $USER_TOKEN" \
  -F 'audio=@./voice.m4a'
```

上传立即返回 `content_id` 和 `status_url`，不会等待 Chromium 渲染。

### 4. 查询状态

```bash
export CONTENT_ID='0123456789abcdef0123456789abcdef'
curl -sS \
  -H "Authorization: Bearer $USER_TOKEN" \
  "http://127.0.0.1:9000/api/v1/contents/$CONTENT_ID"
```

状态可能为 `UPLOADED`、`PROCESSING`、`READY`、`FAILED`、`DELETED`。可重试失败：

```bash
curl -sS -X POST \
  -H "Authorization: Bearer $USER_TOKEN" \
  "http://127.0.0.1:9000/api/v1/contents/$CONTENT_ID/retry"
```

撤销内容：

```bash
curl -i -X DELETE \
  -H "Authorization: Bearer $USER_TOKEN" \
  "http://127.0.0.1:9000/api/v1/contents/$CONTENT_ID"
```

### 5. Trigger 解析 NFC 内容

NFC 写入稳定地址：

```text
https://your-host.example/c/{content_id}
```

Trigger 请求：

```bash
curl -sS \
  -H 'Authorization: Bearer replace-trigger-secret' \
  "http://127.0.0.1:9000/c/$CONTENT_ID"
```

返回完整 Compact Content，包括绝对 video/audio/index URL，不包含用户身份或原始音频信息。

### 6. Playback 下载媒体

```bash
curl -i \
  -H 'Authorization: Bearer replace-playback-secret' \
  -H 'Range: bytes=0-4095' \
  "http://127.0.0.1:9000/api/v1/contents/$CONTENT_ID/assets/video"
```

媒体端点支持 `GET`、`HEAD`、单个 prefix/open/suffix Range 和 `If-Range`。响应使用强 ETag、精确 Content-Length、`Vary: Authorization` 与 private immutable cache。

## Docker

Dockerfile 使用仓库根目录作为构建上下文，以同时构建后端和可视化 renderer：

```bash
cd /home/akira/Projects/advx26
docker build -f backend/Dockerfile -t advx26-cloud-media .

docker run --rm -p 9000:9000 \
  -e BACKEND_TRIGGER_TOKEN='replace-trigger-secret' \
  -e BACKEND_PLAYBACK_TOKEN='replace-playback-secret' \
  -v "$PWD/.scratch/cloud-media-storage:/app/storage" \
  advx26-cloud-media
```

镜像包含 FFmpeg/FFprobe、Node、Puppeteer、Chromium 依赖和已构建 renderer。不要把真实 Token 写入镜像层或命令历史；正式部署使用 secret manager 或受限环境文件。

## OpenAPI 导出

运行时路由、Pydantic 模型和 `app/openapi_config.py` 是规范源。重新生成提交的 OpenAPI 3.1 YAML：

```bash
.venv/bin/python scripts/export_openapi.py
```

生成部署地址版本：

```bash
.venv/bin/python scripts/export_openapi.py \
  --server-url https://media.example.com
```

检查静态规范是否与代码一致：

```bash
.venv/bin/python scripts/export_openapi.py --check
```

## 检查

```bash
.venv/bin/python -m compileall -q app tests scripts
.venv/bin/python scripts/export_openapi.py --check
.venv/bin/python -m unittest discover -s tests -v
```

可视化 renderer 独立验证：

```bash
cd ../Sound-Visualization-Kaleidoscope-effect/particle-field
npm run build
npm run test:renderer
```
