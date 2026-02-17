# main.py
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, Depends, Header
from fastapi.responses import JSONResponse, StreamingResponse

from app.core.config import settings
from app.providers.notion_provider import NotionAIProvider

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

provider = NotionAIProvider()

# --- Session 保活后台任务 ---
async def session_keepalive_task():
    """定期发送保活请求，防止 Notion session 过期"""
    interval = settings.SESSION_KEEPALIVE_INTERVAL
    logger.info(f"Session 保活任务已启动，间隔: {interval} 秒")
    while True:
        await asyncio.sleep(interval)
        try:
            success = await provider.keepalive()
            if not success:
                logger.warning("Session 保活失败，Token 可能已过期！请通过 /admin/update-token 更新。")
        except Exception as e:
            logger.error(f"Session 保活任务异常: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"应用启动中... {settings.APP_NAME} v{settings.APP_VERSION}")
    logger.info("服务已配置为 Notion AI 代理模式。")
    logger.info(f"服务将在 http://localhost:{settings.NGINX_PORT} 上可用")
    # 启动保活后台任务
    keepalive_task = asyncio.create_task(session_keepalive_task())
    yield
    # 关闭时取消保活任务
    keepalive_task.cancel()
    try:
        await keepalive_task
    except asyncio.CancelledError:
        pass
    logger.info("应用关闭。")

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description=settings.DESCRIPTION,
    lifespan=lifespan
)

async def verify_api_key(authorization: Optional[str] = Header(None)):
    if settings.API_MASTER_KEY and settings.API_MASTER_KEY != "1":
        if not authorization or "bearer" not in authorization.lower():
            raise HTTPException(status_code=401, detail="需要 Bearer Token 认证。")
        token = authorization.split(" ")[-1]
        if token != settings.API_MASTER_KEY:
            raise HTTPException(status_code=403, detail="无效的 API Key。")

@app.post("/v1/chat/completions", dependencies=[Depends(verify_api_key)])
async def chat_completions(request: Request) -> StreamingResponse:
    try:
        request_data = await request.json()
        return await provider.chat_completion(request_data)
    except Exception as e:
        logger.error(f"处理聊天请求时发生顶层错误: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"内部服务器错误: {str(e)}")

@app.get("/v1/models", dependencies=[Depends(verify_api_key)], response_class=JSONResponse)
async def list_models():
    return await provider.get_models()

# --- 管理端点（需要 API Key 认证）---

@app.post("/admin/update-token", dependencies=[Depends(verify_api_key)])
async def update_token(request: Request):
    """
    热更新 Notion Token，无需重启容器。
    
    用法:
    curl -X POST https://你的域名/admin/update-token \
      -H "Authorization: Bearer 你的API_KEY" \
      -H "Content-Type: application/json" \
      -d '{"token": "新的token_v2值"}'
    """
    try:
        data = await request.json()
        new_token = data.get("token", "").strip()
        if not new_token:
            raise HTTPException(status_code=400, detail="请提供 token 字段。")
        
        provider.update_token(new_token)
        
        # 验证新 token 是否有效
        is_valid = await provider.keepalive()
        
        return JSONResponse(content={
            "success": True,
            "message": "Token 已更新" + ("，验证通过 ✓" if is_valid else "，但验证失败，请检查 token 是否正确"),
            "token_preview": new_token[:20] + "...",
            "valid": is_valid
        })
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"更新 Token 失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"更新 Token 失败: {str(e)}")

@app.get("/admin/session-status", dependencies=[Depends(verify_api_key)])
async def session_status():
    """检查当前 session 状态"""
    is_alive = await provider.keepalive()
    token_preview = (settings.NOTION_COOKIE or "")[:20] + "..."
    return JSONResponse(content={
        "alive": is_alive,
        "token_preview": token_preview,
        "keepalive_interval": settings.SESSION_KEEPALIVE_INTERVAL,
        "message": "Session 有效 ✓" if is_alive else "Session 已失效 ✗，请更新 Token"
    })

@app.get("/", summary="根路径")
def root():
    return {"message": f"欢迎来到 {settings.APP_NAME} v{settings.APP_VERSION}. 服务运行正常。"}
