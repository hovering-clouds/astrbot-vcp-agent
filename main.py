import asyncio
import base64
import json
import random
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from typing import AsyncGenerator

import httpx

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path


TOOL_REQUEST_RE = re.compile(
    r"<<<\[TOOL_REQUEST\]>>>(.*?)<<<\[END_TOOL_REQUEST\]>>>",
    re.DOTALL,
)
TOOL_PARAM_RE = re.compile(r"([a-zA-Z0-9_]+):「始」(.*?)「末」", re.DOTALL)
MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
HISTORY_HEADER_RE = re.compile(r"^###\s+([A-Z]+)\s+\[([^\]]+)\](?:\s+\((.*?)\))?$")


def _normalize_text_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, dict):
                item_type = item.get("type")
                if item_type == "text":
                    chunks.append(str(item.get("text", "")))
                elif item_type == "image_url":
                    image_obj = item.get("image_url", {})
                    image_url = image_obj.get("url", "")
                    if image_url:
                        chunks.append(f"![图片] {image_url}")
                else:
                    chunks.append(str(item))
            else:
                chunks.append(str(item))
        return "\n".join(chunks).strip()
    return str(content)


def _parse_tool_block(raw_block: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for m in TOOL_PARAM_RE.finditer(raw_block):
        parsed[m.group(1).strip()] = m.group(2).strip()
    return parsed


def _rewrite_tool_calls(content: str, mode: str = "compact") -> tuple[str, list[dict[str, str]]]:
    tool_calls: list[dict[str, str]] = []

    def _replace(match: re.Match) -> str:
        raw = match.group(1)
        parsed = _parse_tool_block(raw)
        tool_calls.append(parsed)
        tool_name = parsed.get("tool_name", "UnknownTool")
        if mode == "verbose":
            args = []
            for k, v in parsed.items():
                if k == "tool_name":
                    continue
                args.append(f"{k}={v}")
            args_text = ", ".join(args)
            if args_text:
                return f"\n[🔧 工具调用] {tool_name}({args_text})\n"
            return f"\n[🔧 工具调用] {tool_name}\n"
        elif mode == "compact":
            return f"\n[🔧 调用工具: {tool_name}]\n"
        else:
            return "\n"

    new_content = TOOL_REQUEST_RE.sub(_replace, content)
    return new_content.strip(), tool_calls


def _extract_image_urls_from_text(text: str) -> tuple[str, list[str]]:
    urls = MD_IMAGE_RE.findall(text)
    cleaned = MD_IMAGE_RE.sub("", text)
    return cleaned.strip(), urls


class ChatHistoryStore:
    def __init__(self, plugin_name: str, configured_path: str | None = None):
        astrbot_data_path = Path(get_astrbot_data_path())
        if configured_path:
            root = Path(configured_path)
            if not root.is_absolute():
                root = (astrbot_data_path / configured_path).resolve()
            self.base_dir = root
        else:
            self.base_dir = astrbot_data_path / "plugin_data" / plugin_name / "conversations"
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _file(self, session_id: str) -> Path:
        safe_name = re.sub(r"[^a-zA-Z0-9_\-]", "_", session_id)
        return self.base_dir / f"{safe_name}.md"

    async def append_entry(
        self,
        session_id: str,
        role: str,
        content: str,
        sender_name: str = "",
        images: list[str] | None = None,
        tool_calls: list[dict[str, str]] | None = None,
    ) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sender_suffix = f" ({sender_name.strip()})" if sender_name and sender_name.strip() else ""
        parts = [
            f"### {role.upper()} [{ts}]{sender_suffix}",
            "",
            content.strip() if content else "",
            "",
        ]
        if images:
            parts.append("#### IMAGES")
            for img in images:
                parts.append(f"- {img}")
            parts.append("")
        if tool_calls:
            parts.append("#### TOOL_CALLS")
            parts.append("```json")
            parts.append(json.dumps(tool_calls, ensure_ascii=False, indent=2))
            parts.append("```")
            parts.append("")
        parts.append("---")
        parts.append("")
        text = "\n".join(parts)
        await asyncio.to_thread(self._append_sync, self._file(session_id), text)

    @staticmethod
    def _append_sync(path: Path, text: str) -> None:
        with path.open("a", encoding="utf-8") as f:
            f.write(text)

    async def load_recent(self, session_id: str, limit: int) -> list[dict[str, str]]:
        path = self._file(session_id)
        if not path.exists():
            return []
        content = await asyncio.to_thread(path.read_text, "utf-8")
        
        blocks = []
        current_block = []
        for line in content.split("\n"):
            if line.strip() == "---":
                if current_block:
                    block_text = "\n".join(current_block).strip()
                    if block_text:
                        blocks.append(block_text)
                    current_block = []
            else:
                current_block.append(line)
        
        if current_block:
            block_text = "\n".join(current_block).strip()
            if block_text:
                blocks.append(block_text)
        
        results: list[dict[str, str]] = []
        for block in blocks:
            lines = block.splitlines()
            if not lines:
                continue
            
            first_line = lines[0]
            role = "assistant"
            sender_name = ""
            timestamp = ""
            m = HISTORY_HEADER_RE.match(first_line.strip())
            if m:
                role = "user" if m.group(1) == "USER" else "assistant"
                timestamp = m.group(2).strip()
                sender_name = (m.group(3) or "").strip()
            elif first_line.startswith("### USER"):
                role = "user"
            
            body_lines = lines[2:] if len(lines) > 2 else []
            body = "\n".join(body_lines).strip()
            
            if "\n#### IMAGES" in body:
                body = body.split("\n#### IMAGES", 1)[0].strip()
            if "\n#### TOOL_CALLS" in body:
                body = body.split("\n#### TOOL_CALLS", 1)[0].strip()
            
            if not sender_name:
                sender_name = "机器人" if role == "assistant" else "用户"
            
            results.append(
                {
                    "role": role,
                    "sender": sender_name,
                    "timestamp": timestamp,
                    "content": body,
                }
            )
        
        if limit <= 0:
            return []
        return results[-limit:]


@register(
    "astrbot_plugin_vcp_agent",
    "hovering-clouds",
    "VCPToolBox Agent Integration",
    "0.1.0",
    "https://github.com/hovering-clouds/astrbot-vcp-agent",
)
class VCPAgentPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or AstrBotConfig()
        self.http_client: httpx.AsyncClient | None = None
        self.history_store: ChatHistoryStore | None = None

    async def initialize(self):
        timeout = httpx.Timeout(90)
        self.http_client = httpx.AsyncClient(timeout=timeout)
        self.history_store = ChatHistoryStore(
            plugin_name=getattr(self, "name", "astrbot_plugin_vcp_agent"),
            configured_path=self.config.get("history_storage_path", "") or None,
        )
        logger.info("[astrbot-vcp-agent] initialized")

    async def terminate(self):
        if self.http_client:
            await self.http_client.aclose()
        logger.info("[astrbot-vcp-agent] terminated")

    def _get_session_id(self, event: AstrMessageEvent) -> str:
        gid = event.get_group_id()
        if gid:
            return f"group_{gid}"
        return f"private_{event.get_sender_id()}"

    def _get_sender_display_name(self, event: AstrMessageEvent) -> str:
        sender_name = (event.get_sender_name() or "").strip()
        if sender_name:
            return sender_name
        return str(event.get_sender_id() or "用户")

    def _build_group_context_prompt(
        self,
        current_message: str,
        current_sender: str,
        recent_history: list[dict[str, str]],
    ) -> str:
        history_lines: list[str] = []
        for item in recent_history:
            content = (item.get("content") or "").strip()
            if not content:
                continue
            sender = (item.get("sender") or item.get("role") or "用户").strip()
            history_lines.append(f"{sender}: {content}")

        history_text = "\n".join(history_lines) if history_lines else "(暂无历史消息)"
        current_text = current_message.strip() or "[仅图片消息]"
        return (
            f"你是一个在线聊天群的成员，现在收到了一条新的消息：[{current_sender}] {current_text}\n"
            f"群聊中最近的消息历史记录为：\n{history_text}\n"
            "请据此做出回复，参与到话题的讨论中或者回应用户的请求。\n"
            f"注意，上述消息中方括号内容由聊天系统自动加入，表示消息的发送者，你在回复时无需使用方括号标注自己名字，直接回复内容即可。"
        )

    def _extract_images_from_event(self, event: AstrMessageEvent) -> list[str]:
        if not self.config.get("enable_image_input", True):
            return []
        images: list[str] = []
        for c in event.get_messages():
            if isinstance(c, Comp.Image):
                url_or_file = c.file or c.url or ""
                if url_or_file:
                    images.append(url_or_file)
        return images

    def _to_data_url_if_local(self, url_or_file: str) -> str:
        if url_or_file.startswith("http://") or url_or_file.startswith("https://"):
            return url_or_file
        if url_or_file.startswith("base64://"):
            return "data:image/png;base64," + url_or_file.removeprefix("base64://")

        path_str = url_or_file
        if path_str.startswith("file:///"):
            path_str = path_str[8:]
        path = Path(path_str)
        if not path.exists():
            return url_or_file

        suffix = path.suffix.lower()
        mime = "image/png"
        if suffix in {".jpg", ".jpeg"}:
            mime = "image/jpeg"
        elif suffix == ".webp":
            mime = "image/webp"
        elif suffix == ".gif":
            mime = "image/gif"

        bs = base64.b64encode(path.read_bytes()).decode("utf-8")
        return f"data:{mime};base64,{bs}"

    def _is_mentioned(self, event: AstrMessageEvent) -> bool:
        return event.is_at_or_wake_command

    def _rule_hit(self, event: AstrMessageEvent) -> bool:
        """检查是否通过群白名单规则。"""
        rules = self.config.get("rules", []) or []
        if not rules:
            return False

        group_id = event.get_group_id()

        for rule in rules:
            if not rule.get("enabled", True):
                continue
            template = rule.get("__template_key", "")
            if template != "group_whitelist":
                continue
            
            allow = {str(x) for x in (rule.get("group_ids", []) or [])}
            if group_id and str(group_id) in allow:
                return True

        return False

    async def _call_vcp(
        self,
        messages: list[dict[str, Any]],
        stream: bool,
    ) -> AsyncGenerator[tuple[str, list[dict[str, str]], list[str]], None]:
        if not self.http_client:
            raise RuntimeError("HTTP client not initialized")

        base_url = str(self.config.get("vcp_base_url", "http://127.0.0.1:8000")).rstrip("/")
        api_key = str(self.config.get("vcp_api_key", ""))
        model = str(self.config.get("model", "gpt-4o-mini"))
        temperature = float(self.config.get("temperature", 0.7) or 0.7)
        mode = str(self.config.get("tool_call_render_mode", "compact"))
        enable_image_output = bool(self.config.get("enable_image_output", True))

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": stream,
        }

        if stream:
            pending = ""
            async with self.http_client.stream(
                "POST",
                f"{base_url}/v1/chat/completions",
                headers=headers,
                json=payload,
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if "error" in obj:
                        error_msg = obj["error"].get("message", str(obj["error"]))
                        raise RuntimeError(f"VCP 服务错误: {error_msg}")
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {})
                    chunk_text = _normalize_text_content(delta.get("content", ""))
                    if not chunk_text:
                        continue
                    pending += chunk_text
                    if "<<<[TOOL_REQUEST]>>>" in pending and "<<<[END_TOOL_REQUEST]>>>" in pending:
                        rewritten, tools = _rewrite_tool_calls(pending, mode=mode)
                        emitted_images: list[str] = []
                        if enable_image_output:
                            rewritten, emitted_images = _extract_image_urls_from_text(rewritten)
                        pending = ""
                        yield rewritten, tools, emitted_images
                
                # 最后剩余的消息
                if pending.strip():
                    rewritten, tools = _rewrite_tool_calls(pending, mode=mode)
                    emitted_images: list[str] = []
                    if enable_image_output:
                        rewritten, emitted_images = _extract_image_urls_from_text(rewritten)
                    yield rewritten, tools, emitted_images
        
        else:
            resp = await self.http_client.post(
                f"{base_url}/v1/chat/completions",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                error_msg = data["error"].get("message", str(data["error"]))
                raise RuntimeError(f"VCP 服务错误: {error_msg}")
            choices = data.get("choices") or []
            if choices:
                message = choices[0].get("message", {})
                raw_text = _normalize_text_content(message.get("content", ""))
                if raw_text:
                    rewritten_full, collected_tools = _rewrite_tool_calls(
                        raw_text,
                        mode=mode,
                    )
                    emitted_images: list[str] = []
                    if enable_image_output:
                        rewritten_full, emitted_images = _extract_image_urls_from_text(rewritten_full)
                    yield rewritten_full, collected_tools, emitted_images

    async def _run_agent(self, event: AstrMessageEvent, prompt: str, skip_llm: bool = False):
        if not self.history_store:
            raise RuntimeError("History store not initialized")

        session_id = self._get_session_id(event)
        sender_name = self._get_sender_display_name(event)
        
        # 记录用户消息
        images = self._extract_images_from_event(event)
        await self.history_store.append_entry(
            session_id=session_id,
            role="user",
            content=prompt.strip() or "[仅图片消息]",
            sender_name=sender_name,
            images=images,
        )
        
        # 如果跳过LLM调用，直接返回
        if skip_llm:
            logger.info(f"[{session_id}] Skipping LLM call (mention_required_in_group enabled but not mentioned)")
            return
        
        history_limit = int(self.config.get("history_window_size", 20) or 20)
        history = await self.history_store.load_recent(session_id=session_id, limit=history_limit)
        
        logger.info(f"[{session_id}] Loaded {len(history)} history entries")
        for i, h in enumerate(history):
            logger.info(f"  [{i}] {h.get('sender', 'N/A')}: {h.get('content', '')[:50]}...")

        discussion_prompt = self._build_group_context_prompt(
            current_message=prompt,
            current_sender=sender_name,
            recent_history=history,
        )
        
        logger.info(f"[{session_id}] Built prompt {discussion_prompt[:100]}...")

        images = self._extract_images_from_event(event)
        user_content: str | list[dict[str, Any]]
        if images:
            content_parts: list[dict[str, Any]] = []
            content_parts.append({"type": "text", "text": discussion_prompt})
            for image in images:
                content_parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": self._to_data_url_if_local(image)},
                    }
                )
            user_content = content_parts
        else:
            user_content = discussion_prompt

        message_list: list[dict[str, Any]] = []
        system_prompt = str(self.config.get("system_prompt", "")).strip()
        if system_prompt:
            message_list.append({"role": "system", "content": system_prompt})
        message_list.append({"role": "user", "content": user_content})

        stream = bool(self.config.get("stream", True))
        raw_text_lst: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        emitted_images: list[str] = []

        async for raw_text, tools, images in self._call_vcp(message_list, stream):
            raw_text_lst.append(raw_text)
            tool_calls.extend(tools)
            emitted_images.extend(images)
            
            chain: list[Any] = []
            if raw_text:
                chain.append(Comp.Plain(raw_text))
            for url in images:
                try:
                    if url.startswith("http://") or url.startswith("https://"):
                        chain.append(Comp.Image.fromURL(url))
                    else:
                        file_path = url[8:] if url.startswith("file:///") else url
                        chain.append(Comp.Image.fromFileSystem(file_path))
                except Exception:
                    chain.append(Comp.Plain(f"[图片地址] {url}"))
            if chain:
                yield event.chain_result(chain)
            else:
                yield event.plain_result("(空响应)")

        await self.history_store.append_entry(
            session_id=session_id,
            role="assistant",
            content="\n".join(raw_text_lst).strip() or "[无文本响应]",
            sender_name=str(event.get_self_id() or "机器人"),
            images=emitted_images,
            tool_calls=tool_calls,
        )

    @filter.command("vcp")
    async def vcp_command(self, event: AstrMessageEvent):
        """手动触发 VCP Agent：/vcp <内容>"""
        prompt = (event.message_str or "").strip()
        if prompt.startswith("/vcp"):
            prompt = prompt[4:].strip()
        if not prompt and not self._extract_images_from_event(event):
            yield event.plain_result("用法：/vcp 你的问题（也支持附带图片）")
            return
        try:
            async for result in self._run_agent(event, prompt):
                yield result
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            if code == 401:
                yield event.plain_result("VCP 鉴权失败：请检查 vcp_api_key。")
            elif code == 403:
                yield event.plain_result("VCP 拒绝访问（可能被黑名单限制）。")
            else:
                yield event.plain_result(f"VCP 请求失败，HTTP {code}。")
        except Exception as e:
            logger.error(f"/vcp 指令失败: {e!s}")
            yield event.plain_result(f"请求失败：{e!s}")

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE | EventMessageType.PRIVATE_MESSAGE)
    async def auto_trigger(self, event: AstrMessageEvent):
        """自动触发：群白名单规则。"""
        text = (event.message_str or "").strip()

        # 基础检查
        if not text and not self._extract_images_from_event(event):
            return
        if text.startswith("/"):
            return
        if event.get_sender_id() and event.get_sender_id() == event.get_self_id():
            return

        # 检查白名单规则（命中失败则完全不处理）
        if not self._rule_hit(event):
            return

        # 白名单命中：确定是否跳过LLM调用
        skip_llm = (
            event.get_group_id()
            and self.config.get("mention_required_in_group", False)
            and not self._is_mentioned(event)
        )
        probability = float(self.config.get("probability", 1.0) or 1.0)
        if random.random() > max(0.0, min(1.0, probability)):
            skip_llm = True

        try:
            async for result in self._run_agent(event, text, skip_llm=skip_llm):
                yield result
        except Exception as e:
            logger.error(f"[astrbot-vcp-agent] auto_trigger failed: {e!s}")
        finally:
            event.stop_event()
