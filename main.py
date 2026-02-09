import os
import time
import asyncio
from typing import AsyncGenerator

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
import aiohttp

# 配置 FastAPI 支持大文件（100MB）
app = FastAPI()
app.state.max_body_size = 100 * 1024 * 1024  # 100 MB

# =========================
# 配置
# =========================
VERSION = "v1.4.0-连接池优化版"  # 版本号，每次更新时修改

# 服务器A的地址（您的 claude-code-hub 主服务）
UPSTREAM_SERVER_A = "https://api.aimasker.com"

# 连接超时设置（秒）
CONNECT_TIMEOUT_SEC = 10  # 减少到 10 秒，连接应该很快
UPSTREAM_TIMEOUT_SEC = 600  # 增加到 10 分钟，支持大图片传输

# 调试日志（True=开启，False=关闭）
DEBUG_LOG = True

# 全局 aiohttp Session（复用连接，提升性能）
_http_session: aiohttp.ClientSession | None = None


def log(msg: str):
    """日志输出"""
    if DEBUG_LOG:
        timestamp = time.strftime("[%Y-%m-%d %H:%M:%S]", time.localtime())
        print(f"{timestamp} {msg}")


async def get_http_session() -> aiohttp.ClientSession:
    """获取全局 HTTP Session（带连接池）"""
    global _http_session
    if _http_session is None or _http_session.closed:
        # 配置 TCP 连接器，启用连接池
        connector = aiohttp.TCPConnector(
            limit=100,  # 最大 100 个连接
            limit_per_host=30,  # 每个主机最多 30 个连接
            ttl_dns_cache=300,  # DNS 缓存 5 分钟
            enable_cleanup_closed=True,
        )
        
        # 配置超时
        timeout = aiohttp.ClientTimeout(
            total=UPSTREAM_TIMEOUT_SEC,
            connect=CONNECT_TIMEOUT_SEC
        )
        
        _http_session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout
        )
        log(f"[init] ✓ HTTP session created with connection pool")
    
    return _http_session


# =========================
# 健康检查和模型列表
# =========================
@app.get("/")
async def root():
    return {"ok": True, "proxy": "Server B → Server A", "version": VERSION}


@app.get("/v1/models")
async def models():
    """返回模型列表（透传到服务器A）"""
    try:
        session = await get_http_session()
        async with session.get(f"{UPSTREAM_SERVER_A}/v1/models") as resp:
            data = await resp.json()
            return JSONResponse(content=data)
    except Exception as e:
        return JSONResponse(
            status_code=502,
            content={"error": f"Failed to fetch models: {repr(e)}"}
        )


@app.get("/{uid}/v1/models")
async def models_with_uid(uid: str):
    """返回模型列表（带 UID）"""
    try:
        session = await get_http_session()
        async with session.get(f"{UPSTREAM_SERVER_A}/{uid}/v1/models") as resp:
            data = await resp.json()
            return JSONResponse(content=data)
    except Exception as e:
        return JSONResponse(
            status_code=502,
            content={"error": f"Failed to fetch models: {repr(e)}"}
        )


@app.get("/debug/info")
async def debug_info():
    """调试信息"""
    return {
        "version": VERSION,
        "proxy_name": "Server B Forwarder",
        "upstream_server_a": UPSTREAM_SERVER_A,
        "connect_timeout_sec": CONNECT_TIMEOUT_SEC,
        "upstream_timeout_sec": UPSTREAM_TIMEOUT_SEC,
    }


# =========================
# 聊天接口（主要逻辑）
# =========================
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
    
    # 检查是否是流式请求
    is_stream = body.get("stream", False)
    
    if is_stream:
        # 流式响应：实时转发，逐块传输
        log(f"[proxy] → streaming request")
        
        async def stream_from_server_a() -> AsyncGenerator[bytes, None]:
            """从服务器A实时流式读取并转发"""
            upstream_url = f"{UPSTREAM_SERVER_A}/{request_path}"
            log(f"[stream] → {upstream_url}")
            
            # 构建请求头
            headers = {
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            }
            
            # 转发 Authorization 头
            auth_header = request_headers.get("Authorization") or request_headers.get("authorization")
            if auth_header:
                headers["Authorization"] = auth_header
                if DEBUG_LOG:
                    auth_preview = auth_header[:20] + "..." if len(auth_header) > 20 else auth_header
                    log(f"[stream] Authorization: {auth_preview}")
            
            try:
                session = await get_http_session()
                async with session.post(upstream_url, headers=headers, json=body) as resp:
                    log(f"[stream] ← connected (status={resp.status})")
                    
                    # 逐块读取并实时转发
                    chunk_count = 0
                    async for chunk in resp.content.iter_any():
                        if chunk:
                            chunk_count += 1
                            yield chunk
                    
                    log(f"[stream] ✓ completed ({chunk_count} chunks)")
            
            except asyncio.TimeoutError:
                log(f"[stream] ✗ timeout after {UPSTREAM_TIMEOUT_SEC}s")
                yield b'data: {"error": "Gateway timeout"}\n\n'
            except Exception as e:
                log(f"[stream] ✗ error: {repr(e)}")
                import traceback
                log(f"[stream] traceback: {traceback.format_exc()}")
                yield f'data: {{"error": "Connection failed: {repr(e)}"}}\n\n'.encode()
        
        return StreamingResponse(
            stream_from_server_a(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # 关闭 Nginx 缓冲
            },
        )
    
    else:
        # 非流式响应：使用同步请求
        log(f"[proxy] → non-streaming request")
        upstream_url = f"{UPSTREAM_SERVER_A}/{request_path}"
        log(f"[forward] → {upstream_url}")
        
        # 构建请求头
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        
        # 转发 Authorization 头
        auth_header = request_headers.get("Authorization") or request_headers.get("authorization")
        if auth_header:
            headers["Authorization"] = auth_header
            if DEBUG_LOG:
                auth_preview = auth_header[:20] + "..." if len(auth_header) > 20 else auth_header
                log(f"[forward] Authorization: {auth_preview}")
        
        try:
            session = await get_http_session()
            async with session.post(upstream_url, headers=headers, json=body) as resp:
                status = resp.status
                
                try:
                    data = await resp.json()
                    log(f"[forward] ← JSON response (status={status})")
                    return JSONResponse(status_code=status, content=data)
                except Exception:
                    text = await resp.text()
                    log(f"[forward] ← text response (status={status})")
                    return Response(status_code=status, content=text)
        
        except asyncio.TimeoutError:
            log(f"[forward] ✗ timeout after {UPSTREAM_TIMEOUT_SEC}s")
            return JSONResponse(
                status_code=504,
                content={"error": "Gateway timeout to Server A"}
            )
        except Exception as e:
            log(f"[forward] ✗ error: {repr(e)}")
            import traceback
            log(f"[forward] traceback: {traceback.format_exc()}")
            return JSONResponse(
                status_code=502,
                content={"error": f"Failed to connect to Server A: {repr(e)}"}
            )


if __name__ == "__main__":
    import uvicorn
    
    # 启动时打印版本和配置信息
    print("=" * 60)
    print(f"🚀 Proxy Forwarder Starting - {VERSION}")
    print("=" * 60)
    print(f"📡 Upstream Server: {UPSTREAM_SERVER_A}")
    print(f"🔌 Listening Port: 8000")
    print(f"⏱️  Connect Timeout: {CONNECT_TIMEOUT_SEC}s")
    print(f"⏱️  Upstream Timeout: {UPSTREAM_TIMEOUT_SEC}s")
    print(f"📝 Debug Log: {'Enabled' if DEBUG_LOG else 'Disabled'}")
    print(f"📦 Max Body Size: 100MB")
    print(f"🌊 Streaming: Real-time chunked transfer")
    print(f"⚡ Connection Pool: Enabled (DNS cache: 5min)")
    print("=" * 60)
    
    uvicorn.run(app, host="0.0.0.0", port=8000)
