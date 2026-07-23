# 多媒体数据包服务端

服务端接收一个由音频、STL 模型、WebM 视频和 JSON 元数据组成的数据包，并提供查询、
HTTP Range 下载和 ZIP 打包分发。文件存储在本地目录，索引存储在 SQLite。

## 启动

```powershell
cd C:\Users\lazy_lz\Desktop\advx26\backend
python -m pip install -r requirements.txt
$env:BACKEND_API_TOKEN = "replace-with-a-strong-token"
python run.py
```

默认监听 `0.0.0.0:8000`。接口文档：`http://127.0.0.1:8000/docs`。

环境变量：

- `BACKEND_API_TOKEN`：上传和删除使用的 Bearer Token；为空时关闭认证，仅适合本地开发。
- `BACKEND_CORS_ORIGINS`：允许的前端来源，逗号分隔；默认为 `*`。

## 上传示例

PowerShell 7 或 Linux/macOS 下可使用 curl：

```bash
curl -X POST http://127.0.0.1:8000/api/v1/packages \
  -H "Authorization: Bearer replace-with-a-strong-token" \
  -F 'metadata={"title":"示例","creator":{"id":"user-1","name":"张三"},"tags":["demo"]}' \
  -F 'audio=@audio.wav;type=audio/wav' \
  -F 'model=@model.stl;type=model/stl' \
  -F 'video=@video.webm;type=video/webm'
```

音频支持 WAV、MP3、Ogg、FLAC、AAC 和 M4A。服务端会检查扩展名、文件头、容量并计算
SHA-256；任何部分失败时，整个数据包均不会提交。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/v1/health` | 进程健康检查 |
| `GET` | `/api/v1/ready` | 数据库和存储就绪检查 |
| `POST` | `/api/v1/packages` | 上传完整数据包 |
| `GET` | `/api/v1/packages` | 分页、创建者、状态和标签筛选 |
| `GET` | `/api/v1/packages/{id}` | 获取完整 manifest |
| `GET` | `/api/v1/packages/{id}/manifest` | 获取完整 manifest |
| `GET/HEAD` | `/api/v1/packages/{id}/files/{audio|model|video}` | 文件及 Range 下载 |
| `GET` | `/api/v1/packages/{id}/bundle` | 下载完整 ZIP |
| `DELETE` | `/api/v1/packages/{id}` | 软删除数据包 |

## 测试

```powershell
python -m unittest discover -s tests -v
```

测试使用临时目录，不会写入正式 `storage`。

