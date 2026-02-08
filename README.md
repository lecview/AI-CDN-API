# OpenAI 格式转发代理（服务器B）

这是一个简单的转发代理服务，用于将本地 OpenAI 格式请求转发到远程的 claude-code-hub 服务（服务器A）。

## 📐 架构说明

```
本地客户端 (Ping: 50ms) 
    ↓
服务器B (本代理，Ping: 10ms) 
    ↓
服务器A (claude-code-hub 主服务，Ping: 500ms)
```

**优势：** 
- ✅ 本地到服务器B延迟低（50ms）
- ✅ 服务器B到服务器A延迟低（10ms）
- ✅ 总延迟从 500ms 降低到 60ms

---

## 🚀 部署步骤（1Panel 面板）

### 1. 上传代码到服务器B

将以下文件上传到服务器B（例如：`/opt/proxy-forwarder/`）：
- `main.py`
- `requirements.txt`
- `.env.example`（复制为 `.env` 并修改配置）

### 2. 配置环境变量

复制 `.env.example` 为 `.env`：
```bash
cp .env.example .env
```

编辑 `.env` 文件，修改服务器A的地址：
```env
UPSTREAM_SERVER_A=http://服务器A的IP:端口
PORT=8000
DEBUG_LOG=1
```

**示例：**
```env
UPSTREAM_SERVER_A=http://123.456.789.100:8080
PORT=8000
```

### 3. 使用 1Panel 部署

#### 方法一：使用 Docker（推荐）

创建 `Dockerfile`（已包含在项目中）：
```dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

CMD ["python", "main.py"]
```

在 1Panel 中：
1. 进入 **容器** → **创建容器**
2. 选择 **构建镜像**，上传 Dockerfile 和相关文件
3. 设置环境变量（从 `.env` 文件复制）
4. 映射端口：`8000:8000`
5. 启动容器

#### 方法二：使用 Python 直接运行

在 1Panel 的终端中：
```bash
cd /opt/proxy-forwarder

# 安装依赖
pip3 install -r requirements.txt

# 启动服务
python3 main.py
```

或使用 systemd 服务持久化运行（见下文）。

---

## 🔧 systemd 服务配置（可选）

创建 `/etc/systemd/system/proxy-forwarder.service`：
```ini
[Unit]
Description=OpenAI Proxy Forwarder (Server B)
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/proxy-forwarder
EnvironmentFile=/opt/proxy-forwarder/.env
ExecStart=/usr/bin/python3 /opt/proxy-forwarder/main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

启动服务：
```bash
systemctl daemon-reload
systemctl enable proxy-forwarder
systemctl start proxy-forwarder
systemctl status proxy-forwarder
```

---

## 📡 本地客户端配置

将本地客户端的 API 地址改为服务器B：

**原来：**
```
http://服务器A的IP:端口/v1/chat/completions
```

**现在：**
```
http://服务器B的IP:8000/v1/chat/completions
```

如果需要使用 UID 路由：
```
http://服务器B的IP:8000/u1/v1/chat/completions
```

---

## 🧪 测试

### 1. 健康检查
```bash
curl http://服务器B的IP:8000/
```

期望输出：
```json
{"ok": true, "proxy": "Server B → Server A"}
```

### 2. 调试信息
```bash
curl http://服务器B的IP:8000/debug/info
```

### 3. 发送聊天请求
```bash
curl -X POST http://服务器B的IP:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{
    "model": "gpt-5.2",
    "messages": [{"role": "user", "content": "Hello"}],
    "stream": false
  }'
```

---

## 📊 日志查看

### Docker 容器
```bash
docker logs -f proxy-forwarder
```

### systemd 服务
```bash
journalctl -u proxy-forwarder -f
```

---

## 🔐 安全建议

1. **防火墙配置**：仅允许您的本地 IP 访问服务器B的 8000 端口
2. **Nginx 反向代理**：可以在 1Panel 中配置 Nginx 添加 HTTPS 和访问控制
3. **API Key 验证**：服务器A的 API Key 验证仍然有效，无需额外配置

---

## ⚙️ 高级配置

### 添加请求日志
编辑 `main.py`，在 `log()` 函数中添加文件日志：
```python
import logging
logging.basicConfig(filename='/var/log/proxy-forwarder.log', level=logging.INFO)
```

### 限流（可选）
安装 `slowapi`：
```bash
pip install slowapi
```

在 `main.py` 中添加：
```python
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

@app.post("/v1/chat/completions")
@limiter.limit("10/minute")
async def chat_default(req: Request):
    ...
```

---

## 📝 常见问题

### Q: 服务器B重启后需要重新启动吗？
A: 如果使用 Docker 或 systemd，会自动启动。

### Q: 如何监控服务状态？
A: 1Panel 面板中可以查看容器状态，或使用 `systemctl status proxy-forwarder`。

### Q: 支持流式响应吗？
A: ✅ 支持！代理会自动检测并转发流式响应。

---

## 📞 故障排查

1. **连接失败** → 检查 `UPSTREAM_SERVER_A` 配置是否正确
2. **超时错误** → 增加 `UPSTREAM_TIMEOUT_SEC` 参数
3. **404 错误** → 确认服务器A的路径是否包含 `/v1/chat/completions`

---

## 📄 许可证

MIT License
