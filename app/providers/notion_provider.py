# app/providers/notion_provider.py
import json
import time
import logging
import uuid
import re
import cloudscraper
from typing import Dict, Any, AsyncGenerator, List, Optional, Tuple
from datetime import datetime

from fastapi import HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.concurrency import run_in_threadpool

from app.core.config import settings
from app.providers.base_provider import BaseProvider
from app.utils.sse_utils import create_sse_data, create_chat_completion_chunk, DONE_CHUNK

# 设置日志记录器
logger = logging.getLogger(__name__)

class NotionAIProvider(BaseProvider):
    def __init__(self):
        self.scraper = cloudscraper.create_scraper()
        self.api_endpoints = {
            "runInference": "https://www.notion.so/api/v3/runInferenceTranscript",
            "saveTransactions": "https://www.notion.so/api/v3/saveTransactionsFanout"
        }
        
        if not all([settings.NOTION_COOKIE, settings.NOTION_SPACE_ID, settings.NOTION_USER_ID]):
            raise ValueError("配置错误: NOTION_COOKIE, NOTION_SPACE_ID 和 NOTION_USER_ID 必须在 .env 文件中全部设置。")

        self._warmup_session()

    def _warmup_session(self):
        try:
            logger.info("正在进行会话预热 (Session Warm-up)...")
            headers = self._prepare_headers()
            headers.pop("Accept", None)
            response = self.scraper.get("https://www.notion.so/", headers=headers, timeout=30)
            response.raise_for_status()
            logger.info("会话预热成功。")
        except Exception as e:
            logger.error(f"会话预热失败: {e}", exc_info=True)

    def update_token(self, new_token: str):
        """热更新 Notion Cookie/Token，无需重启服务"""
        old_token_preview = (settings.NOTION_COOKIE or "")[:20] + "..."
        settings.NOTION_COOKIE = new_token
        # 重新创建 scraper 以清除旧 session
        self.scraper = cloudscraper.create_scraper()
        self._warmup_session()
        new_token_preview = new_token[:20] + "..."
        logger.info(f"Token 已热更新: {old_token_preview} -> {new_token_preview}")

    async def keepalive(self):
        """发送保活请求，维持 Notion session"""
        try:
            headers = self._prepare_headers()
            headers.pop("Accept", None)
            response = await run_in_threadpool(
                lambda: self.scraper.get("https://www.notion.so/api/v3/getSpaces", headers=headers, timeout=30)
            )
            if response.status_code == 200:
                logger.info("Session 保活成功 ✓")
                return True
            else:
                logger.warning(f"Session 保活返回非 200 状态码: {response.status_code}")
                return False
        except Exception as e:
            logger.error(f"Session 保活失败: {e}")
            return False
            
    async def _create_thread(self, thread_type: str) -> str:
        thread_id = str(uuid.uuid4())
        payload = {
            "requestId": str(uuid.uuid4()),
            "transactions": [{
                "id": str(uuid.uuid4()),
                "spaceId": settings.NOTION_SPACE_ID,
                "operations": [{
                    "pointer": {"table": "thread", "id": thread_id, "spaceId": settings.NOTION_SPACE_ID},
                    "path": [],
                    "command": "set",
                    "args": {
                        "id": thread_id, "version": 1, "parent_id": settings.NOTION_SPACE_ID,
                        "parent_table": "space", "space_id": settings.NOTION_SPACE_ID,
                        "created_time": int(time.time() * 1000),
                        "created_by_id": settings.NOTION_USER_ID, "created_by_table": "notion_user",
                        "messages": [], "data": {}, "alive": True, "type": thread_type
                    }
                }]
            }]
        }
        try:
            logger.info(f"正在创建新的对话线程 (type: {thread_type})...")
            response = await run_in_threadpool(
                lambda: self.scraper.post(
                    self.api_endpoints["saveTransactions"],
                    headers=self._prepare_headers(),
                    json=payload,
                    timeout=20
                )
            )
            response.raise_for_status()
            logger.info(f"对话线程创建成功, Thread ID: {thread_id}")
            return thread_id
        except Exception as e:
            logger.error(f"创建对话线程失败: {e}", exc_info=True)
            raise Exception("无法创建新的对话线程。")

    async def chat_completion(self, request_data: Dict[str, Any]):
        stream = request_data.get("stream", True)

        async def stream_generator() -> AsyncGenerator[bytes, None]:
            request_id = f"chatcmpl-{uuid.uuid4()}"
            incremental_fragments: List[str] = []
            final_message: Optional[str] = None
            
            try:
                model_name = request_data.get("model", settings.DEFAULT_MODEL)
                mapped_model = settings.MODEL_MAP.get(model_name, "anthropic-sonnet-alt")
                
                thread_type = "markdown-chat" if mapped_model.startswith("vertex-") else "workflow"
                
                thread_id = await self._create_thread(thread_type)
                payload = self._prepare_payload(request_data, thread_id, mapped_model, thread_type)
                headers = self._prepare_headers()

                role_chunk = create_chat_completion_chunk(request_id, model_name, role="assistant")
                yield create_sse_data(role_chunk)

                def sync_stream_iterator():
                    try:
                        logger.info(f"请求 Notion AI URL: {self.api_endpoints['runInference']}")
                        logger.info(f"请求体: {json.dumps(payload, indent=2, ensure_ascii=False)}")
                        
                        response = self.scraper.post(
                            self.api_endpoints['runInference'], headers=headers, json=payload, stream=True,
                            timeout=settings.API_REQUEST_TIMEOUT
                        )
                        response.raise_for_status()
                        for line in response.iter_lines():
                            if line:
                                yield line
                    except Exception as e:
                        yield e

                sync_gen = sync_stream_iterator()
              
                while True:
                    line = await run_in_threadpool(lambda: next(sync_gen, None))
                    if line is None:
                        break
                    if isinstance(line, Exception):
                        raise line

                    parsed_results = self._parse_ndjson_line_to_texts(line)
                    for text_type, content in parsed_results:
                        if text_type == 'final':
                            final_message = content
                        elif text_type == 'incremental':
                            incremental_fragments.append(content)
              
                # 【修复】优先使用 record-map 的 final 消息（已组装好的干净回复）
                # 增量片段包含模型的思考过程，不应优先使用
                full_response = ""
                if final_message:
                    cleaned_final = self._clean_content(final_message)
                    if cleaned_final:
                        full_response = final_message
                        logger.info(f"使用 final 消息 (清洗后长度={len(cleaned_final)})")
                    else:
                        logger.info(f"final 消息清洗后为空 (原始长度={len(final_message)})，尝试增量拼接")
                
                if not full_response and incremental_fragments:
                    joined = "".join(incremental_fragments)
                    cleaned_joined = self._clean_content(joined)
                    if cleaned_joined:
                        full_response = joined
                        logger.info(f"使用增量拼接消息 (清洗后长度={len(cleaned_joined)})")
                    else:
                        logger.info(f"增量拼接消息清洗后也为空 (原始长度={len(joined)})")

                if full_response:
                    cleaned_response = self._clean_content(full_response)
                    logger.info(f"清洗前长度={len(full_response)}, 清洗后长度={len(cleaned_response)}")
                    logger.info(f"清洗后的最终响应: {cleaned_response[:200]}...")
                    chunk = create_chat_completion_chunk(request_id, model_name, content=cleaned_response)
                    yield create_sse_data(chunk)
                else:
                    logger.warning("警告: Notion 返回的数据流中未提取到任何有效文本。请检查您的 .env 配置是否全部正确且凭证有效。")

                final_chunk = create_chat_completion_chunk(request_id, model_name, finish_reason="stop")
                yield create_sse_data(final_chunk)
                yield DONE_CHUNK

            except Exception as e:
                error_message = f"处理 Notion AI 流时发生意外错误: {str(e)}"
                logger.error(error_message, exc_info=True)
                error_chunk = {"error": {"message": error_message, "type": "internal_server_error"}}
                yield create_sse_data(error_chunk)
                yield DONE_CHUNK

        if stream:
            return StreamingResponse(stream_generator(), media_type="text/event-stream")
        else:
            raise HTTPException(status_code=400, detail="此端点当前仅支持流式响应 (stream=true)。")

    def _prepare_headers(self) -> Dict[str, str]:
        cookie_source = (settings.NOTION_COOKIE or "").strip()
        cookie_header = cookie_source if "=" in cookie_source else f"token_v2={cookie_source}"

        return {
            "Content-Type": "application/json",
            "Accept": "application/x-ndjson",
            "Cookie": cookie_header,
            "x-notion-space-id": settings.NOTION_SPACE_ID,
            "x-notion-active-user-header": settings.NOTION_USER_ID,
            "x-notion-client-version": settings.NOTION_CLIENT_VERSION,
            "notion-audit-log-platform": "web",
            "Origin": "https://www.notion.so",
            "Referer": "https://www.notion.so/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        }

    def _normalize_block_id(self, block_id: str) -> str:
        if not block_id: return block_id
        b = block_id.replace("-", "").strip()
        if len(b) == 32 and re.fullmatch(r"[0-9a-fA-F]{32}", b):
            return f"{b[0:8]}-{b[8:12]}-{b[12:16]}-{b[16:20]}-{b[20:]}"
        return block_id

    def _prepare_payload(self, request_data: Dict[str, Any], thread_id: str, mapped_model: str, thread_type: str) -> Dict[str, Any]:
        req_block_id = request_data.get("notion_block_id") or settings.NOTION_BLOCK_ID
        normalized_block_id = self._normalize_block_id(req_block_id) if req_block_id else None

        context_value: Dict[str, Any] = {
            "timezone": "Asia/Shanghai",
            "spaceId": settings.NOTION_SPACE_ID,
            "userId": settings.NOTION_USER_ID,
            "userEmail": settings.NOTION_USER_EMAIL,
            "currentDatetime": datetime.now().astimezone().isoformat(),
        }
        if normalized_block_id:
            context_value["blockId"] = normalized_block_id

        config_value: Dict[str, Any]
        
        if mapped_model.startswith("vertex-"):
            logger.info(f"检测到 Gemini 模型 ({mapped_model})，应用特定的 config 和 context。")
            context_value.update({
                "userName": f" {settings.NOTION_USER_NAME}",
                "spaceName": f"{settings.NOTION_USER_NAME}的 Notion",
                "spaceViewId": "2008eefa-d0dc-80d5-9e67-000623befd8f",
                "surface": "ai_module"
            })
            config_value = {
                "type": thread_type,
                "model": mapped_model,
                "useWebSearch": True,
                "enableAgentAutomations": False, "enableAgentIntegrations": False,
                "enableBackgroundAgents": False, "enableCodegenIntegration": False,
                "enableCustomAgents": False, "enableExperimentalIntegrations": False,
                "enableLinkedDatabases": False, "enableAgentViewVersionHistoryTool": False,
                "searchScopes": [{"type": "everything"}], "enableDatabaseAgents": False,
                "enableAgentComments": False, "enableAgentForms": False,
                "enableAgentMakesFormulas": False, "enableUserSessionContext": False,
                "modelFromUser": True, "isCustomAgent": False
            }
        else:
            context_value.update({
                "userName": settings.NOTION_USER_NAME,
                "surface": "workflows"
            })
            config_value = {
                "type": thread_type,
                "model": mapped_model,
                "useWebSearch": True,
            }

        transcript = [
            {"id": str(uuid.uuid4()), "type": "config", "value": config_value},
            {"id": str(uuid.uuid4()), "type": "context", "value": context_value}
        ]
        # 【修复】预处理消息：合并连续的同角色消息，处理 system 消息
        # Notion 要求 user 和 agent-inference 严格交替，连续同角色会导致 error
        messages = request_data.get("messages", [])
        
        system_prompt = ""
        normalized_msgs = []
        
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if not content:
                continue
            
            if role == "system":
                # system 消息合并到第一条 user 消息前
                system_prompt += content + "\n"
            elif role in ("user", "assistant"):
                # 检查是否与上一条消息同角色，如果是则合并
                if normalized_msgs and normalized_msgs[-1]["role"] == role:
                    normalized_msgs[-1]["content"] += "\n" + content
                else:
                    normalized_msgs.append({"role": role, "content": content})
        
        # 将 system prompt 合并到第一条 user 消息
        if system_prompt and normalized_msgs and normalized_msgs[0]["role"] == "user":
            normalized_msgs[0]["content"] = system_prompt + normalized_msgs[0]["content"]
        elif system_prompt:
            # 如果第一条不是 user，在最前面插入一条 user 消息
            normalized_msgs.insert(0, {"role": "user", "content": system_prompt.strip()})
        
        # 确保消息以 user 结尾（Notion 需要最后一条是 user 提问）
        # 如果最后一条是 assistant，移除它
        while normalized_msgs and normalized_msgs[-1]["role"] == "assistant":
            normalized_msgs.pop()
        
        for msg in normalized_msgs:
            if msg["role"] == "user":
                transcript.append({
                    "id": str(uuid.uuid4()),
                    "type": "user",
                    "value": [[msg["content"]]],
                    "userId": settings.NOTION_USER_ID,
                    "createdAt": datetime.now().astimezone().isoformat()
                })
            elif msg["role"] == "assistant":
                transcript.append({"id": str(uuid.uuid4()), "type": "agent-inference", "value": [{"type": "text", "content": msg["content"]}]})

        payload = {
            "traceId": str(uuid.uuid4()),
            "spaceId": settings.NOTION_SPACE_ID,
            "transcript": transcript,
            "threadId": thread_id,
            "createThread": False,
            "isPartialTranscript": True,
            "asPatchResponse": True,
            "generateTitle": True,
            "saveAllThreadOperations": True,
            "threadType": thread_type
        }

        if mapped_model.startswith("vertex-"):
            logger.info("为 Gemini 请求添加 debugOverrides。")
            payload["debugOverrides"] = {
                "emitAgentSearchExtractedResults": True,
                "cachedInferences": {},
                "annotationInferences": {},
                "emitInferences": False
            }
        
        return payload


    def _clean_content(self, content: str) -> str:
        if not content:
            return ""
        
        # 如果内容包含 <lang primary="..." /> 标签，
        # 则标签之前的内容是模型的思考过程，只保留标签之后的实际响应。
        lang_match = re.search(r'<lang\s+primary="[^"]*"\s*/?>', content)
        if lang_match:
            after_lang = content[lang_match.end():].strip()
            if after_lang:
                content = after_lang
        
        # 清除残留的 <lang 标签片段
        content = re.sub(r'<lang\b[^>]*(?:>|$)', '', content)
        # 清除残留的 primary="..." 片段
        content = re.sub(r'^\s*primary="[^"]*"\s*[-–]?\s*', '', content)
        
        # 清除 <websearch>...</websearch> 工具调用标签（AI 内部指令，非用户内容）
        content = re.sub(r'<websearch>[\s\S]*?</websearch>\s*', '', content, flags=re.IGNORECASE)
        
        # 清除 thinking 标签
        content = re.sub(r'<thinking>[\s\S]*?</thinking>\s*', '', content, flags=re.IGNORECASE)
        content = re.sub(r'<thought>[\s\S]*?</thought>\s*', '', content, flags=re.IGNORECASE)
        

        
        return content.strip()

    def _parse_ndjson_line_to_texts(self, line: bytes) -> List[Tuple[str, str]]:
        results: List[Tuple[str, str]] = []
        try:
            s = line.decode("utf-8", errors="ignore").strip()
            if not s: return results
            
            data = json.loads(s)
            data_type = data.get("type", "unknown")
            
            # 【调试】打印每行 NDJSON 的类型和关键信息
            if data_type == "patch":
                ops = data.get("v", [])
                for op in ops:
                    if isinstance(op, dict):
                        logger.info(f"[NDJSON] type=patch, o={op.get('o')}, p={op.get('p')}, v_type={type(op.get('v')).__name__}, v_preview={str(op.get('v', ''))[:150]}")
            elif data_type == "record-map":
                logger.info(f"[NDJSON] type=record-map, keys={list(data.get('recordMap', {}).keys())}")
            else:
                logger.info(f"[NDJSON] type={data_type}, keys={list(data.keys())[:5]}")
            
            # 格式1: Gemini 返回的 markdown-chat 事件
            if data.get("type") == "markdown-chat":
                content = data.get("value", "")
                if content:
                    logger.info("从 'markdown-chat' 直接事件中提取到内容。")
                    results.append(('final', content))

            # 格式2: Claude 和 GPT 返回的补丁流，以及 Gemini 的 patch 格式
            elif data.get("type") == "patch" and "v" in data:
                for operation in data.get("v", []):
                    if not isinstance(operation, dict): continue
                    
                    op_type = operation.get("o")
                    path = operation.get("p", "")
                    value = operation.get("v")
                    
                    # Gemini 的完整内容 patch 格式
                    if op_type == "a" and path.endswith("/s/-") and isinstance(value, dict) and value.get("type") == "markdown-chat":
                        content = value.get("value", "")
                        if content:
                            logger.info("从 'patch' (Gemini-style) 中提取到完整内容。")
                            results.append(('final', content))
                    
                    # Gemini 的增量内容 patch 格式
                    elif op_type == "x" and "/s/" in path and path.endswith("/value") and isinstance(value, str):
                        content = value
                        if content:
                            results.append(('incremental', content))
                    
                    # Claude 和 GPT 的增量内容 patch 格式
                    elif op_type == "x" and "/value/" in path and isinstance(value, str):
                        content = value
                        if content:
                            results.append(('incremental', content))
                    
                    # Claude 和 GPT 的完整内容 patch 格式
                    elif op_type == "a" and path.endswith("/value/-") and isinstance(value, dict) and value.get("type") == "text":
                        content = value.get("content", "")
                        if content:
                            logger.info("从 'patch' (Claude/GPT-style) 中提取到完整内容。")
                            results.append(('final', content))

            # 格式3: 处理record-map类型的数据
            elif data.get("type") == "record-map" and "recordMap" in data:
                record_map = data["recordMap"]
                if "thread_message" in record_map:
                    # 【修复】只选取最后一条 user 消息之后的 agent-inference
                    # record-map 包含完整对话历史，历史中的 agent-inference 不是新回复
                    all_messages = []
                    msg_count = 0
                    for msg_id, msg_data in record_map["thread_message"].items():
                        msg_count += 1
                        value_data = msg_data.get("value", {}).get("value", {})
                        step = value_data.get("step", {})
                        
                        if not step:
                            logger.info(f"[record-map] msg_id={msg_id}, no step, value keys={list(value_data.keys())[:5]}")
                            all_messages.append({"step_type": "unknown", "content": ""})
                            continue

                        step_type = step.get("type")
                        logger.info(f"[record-map] msg_id={msg_id}, step_type={step_type}")
                        
                        content = ""
                        if step_type == "markdown-chat":
                            content = step.get("value", "")
                        elif step_type == "agent-inference":
                            agent_values = step.get("value", [])
                            text_parts = []
                            if isinstance(agent_values, list):
                                for item in agent_values:
                                    if isinstance(item, dict) and item.get("type") == "text":
                                        text_content = item.get("content", "")
                                        if text_content:
                                            text_parts.append(text_content)
                                            logger.info(f"[record-map] text item (长度={len(text_content)}): {text_content[:100]}...")
                            content = "\n".join(text_parts) if text_parts else ""
                        
                        if content and isinstance(content, str):
                            logger.info(f"从 record-map (type: {step_type}) 发现消息 (长度={len(content)}): {content[:120]}...")
                        
                        all_messages.append({"step_type": step_type, "content": content})
                    
                    logger.info(f"[record-map] 共扫描 {msg_count} 条 thread_message")
                    
                    # 找到最后一条 user 消息的位置
                    last_user_idx = -1
                    for i, msg in enumerate(all_messages):
                        if msg["step_type"] == "user":
                            last_user_idx = i
                    
                    # 只在最后一条 user 消息之后搜索 agent-inference
                    new_content = ""
                    new_step_type = ""
                    search_start = last_user_idx + 1 if last_user_idx >= 0 else 0
                    for i in range(search_start, len(all_messages)):
                        msg = all_messages[i]
                        if msg["step_type"] in ("agent-inference", "markdown-chat") and msg["content"]:
                            new_content = msg["content"]
                            new_step_type = msg["step_type"]
                    
                    if new_content:
                        logger.info(f"从 record-map 选择最后一条 user 之后的新消息 (type: {new_step_type})。")
                        results.append(('final', new_content))
                    elif last_user_idx >= 0:
                        # 最后一条 user 之后没有 agent-inference，可能是 error
                        logger.warning(f"[record-map] 最后一条 user 之后没有找到有效的 agent-inference（可能 Notion 返回了 error）")
    
        except (json.JSONDecodeError, AttributeError) as e:
            logger.warning(f"解析NDJSON行失败: {e} - Line: {line.decode('utf-8', errors='ignore')}")
        
        return results

    async def get_models(self) -> JSONResponse:
        model_data = {
            "object": "list",
            "data": [
                {"id": name, "object": "model", "created": int(time.time()), "owned_by": "lzA6"}
                for name in settings.KNOWN_MODELS
            ]
        }
        return JSONResponse(content=model_data)
