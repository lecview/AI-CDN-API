import os
import time
import asyncio
from typing import AsyncGenerator

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
import aiohttp

app = FastAPI()

# =========================
# 配置
# =========================
# 服务器A的地址（您的 claude-code-hub 主服务）
UPSTREAM_SERVER_A = "https://api.aimasker.com"

# 连接超时设置（秒）
CONNECT_TIMEOUT_SEC = 20
UPSTREAM_TIMEOUT_SEC = 300

# 调试日志（True=开启，False=关闭）
DEBUG_LOG = True


def log(msg: str):
    """打印调试日志"""
    if DEBUG_LOG:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# =========================
# 健康检查和模型列表
# =========================
@app.get("/")
async def root():
    return {"ok": True, "proxy": "Server B → Server A"}


@app.get("/v1/models")
async def list_models():
    """返回模型列表（可选：也可转发给服务器A）"""
    return {
        "object": "list",
        "data": [
            {"id": "gpt-5.2", "object": "model", "owned_by": "openai"},
            {"id": "gpt-5.2-mini", "object": "model", "owned_by": "openai"},
        ],
    }


@app.get("/{uid}/v1/models")
async def list_models_uid(uid: str):
    """带 UID 的模型列表"""
    return await list_models()


@app.head("/{uid}")
async def head_uid(uid: str):
    return Response(status_code=200)


@app.get("/{uid}")
async def get_uid(uid: str):
    return {"ok": True, "uid": uid, "proxy": "Server B"}


@app.get("/debug/info")
async def debug_info():
    """调试信息"""
    return {
        "proxy_name": "Server B Forwarder",
        "upstream_server_a": UPSTREAM_SERVER_A,
        "connect_timeout_sec": CONNECT_TIMEOUT_SEC,
        "upstream_timeout_sec": UPSTREAM_TIMEOUT_SEC,
    }


# =========================
# 核心代理逻辑：透传请求到服务器A
# =========================
async def forward_to_server_a(
    request_path: str,
    request_headers: dict,
    request_body: dict,
) -> tuple[int, dict | str, dict]:
    """
    转发请求到服务器A
    返回：(status_code, response_body, response_headers)
    """
    # 构建上游URL
    upstream_url = f"{UPSTREAM_SERVER_A.rstrip('/')}/{request_path.lstrip('/')}"
    
    log(f"[forward] → {upstream_url}")
    log(f"[forward] body: {request_body}")
    
    # 准备请求头（移除不必要的头）
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    
    # 转发 Authorization 头
    auth_header = request_headers.get("Authorization") or request_headers.get("authorization")
    if auth_header:
        headers["Authorization"] = auth_header
        log(f"[forward] Authorization: {auth_header[:20]}..." if len(auth_header) > 20 else f"[forward] Authorization: {auth_header}")
    else:
        log(f"[forward] ⚠️ No Authorization header found!")
    
    # 设置超时
    timeout = aiohttp.ClientTimeout(
        total=UPSTREAM_TIMEOUT_SEC,
        connect=CONNECT_TIMEOUT_SEC,
        sock_connect=CONNECT_TIMEOUT_SEC,
        sock_read=UPSTREAM_TIMEOUT_SEC,
    )
    
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(upstream_url, headers=headers, json=request_body) as resp:
                status = resp.status
                content_type = resp.headers.get("Content-Type", "application/json")
                
                # 如果是流式响应，返回原始响应
                if "text/event-stream" in content_type:
                    log(f"[forward] ← streaming response (status={status})")
                    # 读取所有流式数据
                    stream_data = await resp.read()
                    return (status, stream_data, dict(resp.headers))
                else:
                    # 非流式响应
                    try:
                        data = await resp.json()
                        log(f"[forward] ← JSON response (status={status})")
                        return (status, data, dict(resp.headers))
                    except Exception:
                        text = await resp.text()
                        log(f"[forward] ← text response (status={status})")
                        return (status, text, dict(resp.headers))
    
    except asyncio.TimeoutError:
        log(f"[forward] ✗ timeout")
        return (504, {"error": "Gateway timeout to Server A"}, {})
    except Exception as e:
        log(f"[forward] ✗ error: {repr(e)}")
        return (502, {"error": f"Failed to connect to Server A: {repr(e)}"}, {})


@app.post("/v1/chat/completions")
async def chat_default(req: Request):
    """默认聊天接口"""
    return await chat_proxy(None, req)


@app.post("/{uid}/v1/chat/completions")
async def chat_proxy(uid: str | None, req: Request):
    """带 UID 的聊天接口"""
    try:
        body = await req.json()
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={"error": f"Invalid JSON: {repr(e)}"}
        )
    
    # 构建请求路径
    if uid:
        request_path = f"{uid}/v1/chat/completions"
    else:
        request_path = "v1/chat/completions"
    
    # 获取请求头
    request_headers = dict(req.headers)
    
    # 转发到服务器A
    status, response_body, response_headers = await forward_to_server_a(
        request_path, request_headers, body
    )
    
    # 处理流式响应
    if isinstance(response_body, bytes) and b"data:" in response_body:
        log(f"[proxy] → streaming response to client")
        
        async def stream_generator() -> AsyncGenerator[bytes, None]:
            # 直接返回从服务器A获取的流式数据
            yield response_body
        
        return StreamingResponse(
            stream_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    
    # 返回普通响应
    if isinstance(response_body, dict):
        return JSONResponse(status_code=status, content=response_body)
    else:
        return Response(status_code=status, content=response_body)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
