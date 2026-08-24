"""Local OpenAI-compatible API for OpenCode.

Model mapping:
  default -> DeepSeek web ``model_type=default`` (v4 Flash in this setup)
  extra   -> DeepSeek web ``model_type=expert`` (v4 Pro in this setup)
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import re
import time
import uuid
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from deepseek_playwright import AccountSuspendedError, AuthenticationFailedError, completion_events, create_session, client_headers, load_cookies, pow_headers
from playwright.async_api import async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError


load_dotenv()
BASE_URL = "https://chat.deepseek.com"
COOKIE_FILE = os.getenv("DEEPSEEK_COOKIES", "cookies.json")
MODEL_TYPES = {"default": "default", "extra": "expert"}
app = FastAPI(title="DeepSeek local OpenCode backend")


class ChatRequest(BaseModel):
    model: str = "default"
    messages: list[dict[str, Any]] = Field(default_factory=list)
    stream: bool = False
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any = None
    temperature: float | None = None
    thinking_enabled: bool | None = None
    search_enabled: bool | None = None


def prompt_from_messages(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> str:
    parts: list[str] = []
    if tools:
        definitions = []
        for tool in tools:
            function = tool.get("function", tool)
            definitions.append(json.dumps(function, ensure_ascii=False, separators=(",", ":")))
        parts.append(
            "You may call tools using exactly this DSML format when needed:\n"
            "<|DSML|tool_calls><|DSML|invoke name=\"TOOL_NAME\"><|DSML|parameter name=\"ARG\">"
            "VALUE</|DSML|parameter></|DSML|invoke></|DSML|tool_calls>\n"
            "Available tools:\n" + "\n".join(definitions)
        )
    for message in messages:
        role = message.get("role", "user")
        content = message.get("content", "")
        if isinstance(content, list):
            content = " ".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
        if role == "tool":
            parts.append(f"Tool result ({message.get('name', '')}): {content}")
        elif role == "assistant" and message.get("tool_calls"):
            parts.append(f"Assistant tool calls: {json.dumps(message['tool_calls'], ensure_ascii=False)}")
        else:
            parts.append(f"{role.capitalize()}: {content}")
    return "\n\n".join(parts)


def extract_text(events: list[dict[str, Any]]) -> tuple[str, str]:
    text: list[str] = []
    thinking: list[str] = []
    fragment = "content"
    for event in events:
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        path, op, value = data.get("p", ""), data.get("o", ""), data.get("v", "")
        if isinstance(value, dict) and isinstance(value.get("response"), dict):
            response = value["response"]
            fragments = response.get("fragments", [])
            if fragments:
                fragment = "thinking" if fragments[0].get("type") == "THINK" else "content"
                initial = fragments[0].get("content", "")
                (thinking if fragment == "thinking" else text).append(initial)
            else:
                # Quick-mode responses may carry their first characters in
                # response.content instead of a fragments array.
                initial = response.get("content", "")
                if initial:
                    text.append(str(initial))
        elif path == "response/fragments" and op == "APPEND" and isinstance(value, list) and value:
            fragment = "thinking" if value[0].get("type") == "THINK" else "content"
        elif path == "response/content" and op == "APPEND":
            text.append(str(value))
        elif path == "response/fragments/-1/content":
            (thinking if fragment == "thinking" else text).append(str(value))
        elif "v" in data and "p" not in data and "o" not in data and isinstance(value, str):
            (thinking if fragment == "thinking" else text).append(value)
    return "".join(text), "".join(thinking)


def parse_tool_calls(text: str) -> tuple[str, list[dict[str, Any]]]:
    if not text:
        return text, []

    # DeepSeek can split or slightly normalize the DSML marker while
    # streaming. Normalize all observed forms before parsing the XML-like
    # protocol, otherwise the whole tool call leaks as assistant text.
    normalized = text.replace("|DSML|", "")
    normalized = html.unescape(normalized)
    invoke_pattern = re.compile(
        r"<invoke\s+name\s*=\s*([\"'])(.*?)\1\s*>(.*?)</invoke>", re.S | re.I
    )
    parameter_pattern = re.compile(
        r"<parameter\s+name\s*=\s*([\"'])(.*?)\1\s*>(.*?)</parameter>",
        re.S | re.I,
    )
    calls = []
    for match in invoke_pattern.finditer(normalized):
        arguments: dict[str, Any] = {}
        for parameter in parameter_pattern.finditer(match.group(3)):
            name, value = parameter.group(2), parameter.group(3)
            value = value.strip()
            if value.startswith("<![CDATA[") and value.endswith("]]>"):
                value = value[9:-3]
            try:
                arguments[name] = json.loads(value)
            except json.JSONDecodeError:
                arguments[name] = value
        calls.append({
            "id": f"call_{uuid.uuid4().hex[:12]}",
            "type": "function",
            "function": {
                "name": match.group(2).strip(),
                "arguments": json.dumps(arguments, ensure_ascii=False),
            },
        })
    clean = re.sub(r"<tool_calls?>.*?</tool_calls?>", "", normalized, flags=re.S | re.I)
    clean = re.sub(r"<invoke[^>]*>.*?</invoke>", "", clean, flags=re.S | re.I)
    clean = re.sub(r"<parameter[^>]*>.*?</parameter>", "", clean, flags=re.S | re.I)
    clean = re.sub(r"\[citation:\d+\]", "", clean)
    return clean, calls


async def upstream(request: ChatRequest) -> tuple[str, str, list[dict[str, Any]]]:
    token = os.getenv("DEEPSEEK_BEARER_TOKEN")
    if not token:
        raise RuntimeError("DEEPSEEK_BEARER_TOKEN is not set")
    if request.model not in MODEL_TYPES:
        raise HTTPException(400, f"Unknown model {request.model}; use default or extra")
    # Both current web modes can emit THINK fragments. Keep reasoning enabled
    # unless the caller explicitly disables it.
    thinking = request.thinking_enabled if request.thinking_enabled is not None else True
    search = request.search_enabled if request.search_enabled is not None else False
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=os.getenv("DEEPSEEK_USER_AGENT", "Mozilla/5.0"))
        await context.add_cookies(load_cookies(COOKIE_FILE))
        page = await context.new_page()
        try:
            try:
                await page.goto(BASE_URL + "/", wait_until="commit", timeout=15_000)
            except PlaywrightTimeoutError:
                pass
            headers = await pow_headers(page, client_headers(token, os.getenv("DEEPSEEK_LOCALE", "ru")))
            session_id = await create_session(page, headers)
            headers = await pow_headers(page, client_headers(token, os.getenv("DEEPSEEK_LOCALE", "ru")))
            events = [event async for event in completion_events(page, headers, session_id, prompt_from_messages(request.messages, request.tools), model_type=MODEL_TYPES[request.model], thinking=thinking, search=search)]
        finally:
            await browser.close()
    text, reasoning = extract_text(events)
    text, calls = parse_tool_calls(text)
    # Some expert responses can finish while only the THINK fragment has
    # arrived. Do not return an invalid empty assistant message to OpenCode.
    if not text and reasoning and not calls:
        text = reasoning
        reasoning = ""
    if not text and not reasoning and not calls:
        event_kinds = [event.get("event", "data") for event in events[:12]]
        raise RuntimeError(
            "DeepSeek returned an empty completion; "
            f"received {len(events)} SSE events ({event_kinds}). "
            "The session may be rate-limited or expired."
        )
    return text, reasoning, calls


def response_id() -> str:
    return "chatcmpl-" + uuid.uuid4().hex


@app.get("/v1/models")
async def models() -> dict[str, Any]:
    now = int(time.time())
    return {"object": "list", "data": [{"id": name, "object": "model", "created": now, "owned_by": "deepseek-web"} for name in MODEL_TYPES]}


@app.post("/v1/chat/completions")
async def completions(request: ChatRequest) -> Any:
    try:
        text, reasoning, calls = await upstream(request)
    except AuthenticationFailedError as exc:
        print(f"deepseek authentication failed: {exc}", flush=True)
        raise HTTPException(status_code=401, detail={
            "error": "invalid_token",
            "message": str(exc),
            "hint": "Refresh DEEPSEEK_BEARER_TOKEN and cookies.json from the current browser session.",
        }) from exc
    except AccountSuspendedError as exc:
        print(f"account suspended: {exc}", flush=True)
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        print(f"completion error: {type(exc).__name__}: {exc}", flush=True)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    rid = response_id()
    if request.stream:
        async def stream():
            if reasoning:
                yield f"data: {json.dumps({'id': rid, 'object': 'chat.completion.chunk', 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'reasoning_content': reasoning}, 'finish_reason': None}]}, ensure_ascii=False)}\n\n"
            if calls:
                # OpenAI-compatible tool streaming requires one indexed delta
                # per call. Sending the complete array in one delta causes
                # OpenCode to truncate long path arguments.
                for index, call in enumerate(calls):
                    function = call["function"]
                    start_delta = {
                        "tool_calls": [{
                            "index": index,
                            "id": call["id"],
                            "type": "function",
                            "function": {"name": function["name"], "arguments": ""},
                        }]
                    }
                    yield f"data: {json.dumps({'id': rid, 'object': 'chat.completion.chunk', 'choices': [{'index': 0, 'delta': start_delta, 'finish_reason': None}]}, ensure_ascii=False)}\n\n"
                    arguments = function["arguments"]
                    for offset in range(0, len(arguments), 256):
                        delta = {"tool_calls": [{"index": index, "function": {"arguments": arguments[offset:offset + 256]}}]}
                        yield f"data: {json.dumps({'id': rid, 'object': 'chat.completion.chunk', 'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]}, ensure_ascii=False)}\n\n"
            else:
                delta = {"role": "assistant", "content": text}
                yield f"data: {json.dumps({'id': rid, 'object': 'chat.completion.chunk', 'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'id': rid, 'object': 'chat.completion.chunk', 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls' if calls else 'stop'}]}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(stream(), media_type="text/event-stream")
    message: dict[str, Any] = {"role": "assistant", "content": text or None}
    if calls:
        message["tool_calls"] = calls
    return JSONResponse({"id": rid, "object": "chat.completion", "created": int(time.time()), "model": request.model, "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if calls else "stop"}], "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})


if __name__ == "__main__":
    import uvicorn
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.getenv("OPENCODE_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("OPENCODE_PORT", "8787")))
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)
