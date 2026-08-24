"""Minimal Playwright client for the DeepSeek web chat API.

The script deliberately reads authentication data from environment variables.
Do not commit real cookies or Bearer tokens to source control.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import codecs
import json
import os
import sys
import threading
import struct
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator

from dotenv import load_dotenv
from playwright.async_api import (
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

try:
    from wasmtime import Instance, Module, Store
except ImportError:  # pragma: no cover
    Instance = Module = Store = None

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None


BASE_URL = "https://chat.deepseek.com"
CREATE_PATH = "/api/v0/chat_session/create"
COMPLETION_PATH = "/api/v0/chat/completion"
POW_PATH = "/api/v0/chat/create_pow_challenge"
HIF_LEIM_URL = "https://hif-leim.deepseek.com/query"
WASM_URL = (
    "https://raw.githubusercontent.com/snake-aabb-wtf/"
    "deepseek-web2api-free/main/sha3_wasm_bg.wasm"
)


class AccountSuspendedError(RuntimeError):
    """The upstream account is temporarily suspended or muted."""


class AuthenticationFailedError(RuntimeError):
    """The DeepSeek Bearer token is missing, expired, or invalid."""


def suspension_message(value: Any) -> str | None:
    """Find DeepSeek suspension text in JSON, SSE data, or nested values."""
    if isinstance(value, dict):
        for item in value.values():
            found = suspension_message(item)
            if found:
                return found
        return None
    if isinstance(value, list):
        for item in value:
            found = suspension_message(item)
            if found:
                return found
        return None
    if not isinstance(value, str):
        return None
    lowered = value.lower()
    markers = ("account has been suspended", "account is suspended", "user is muted", "账号已被封禁", "账号已被禁用")
    if any(marker in lowered for marker in markers):
        return value.strip()
    return None


def authentication_message(value: Any) -> str | None:
    """Find DeepSeek's invalid-token response in nested API data."""
    if isinstance(value, dict):
        code = value.get("code")
        message = " ".join(str(value.get(key, "")) for key in ("msg", "message", "detail")).lower()
        if code == 40003 or "authorization failed" in message or "invalid token" in message:
            return str(value.get("msg") or value.get("message") or "DeepSeek authorization failed")
        for item in value.values():
            found = authentication_message(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = authentication_message(item)
            if found:
                return found
    elif isinstance(value, str):
        lowered = value.lower()
        if "authorization failed" in lowered or "invalid token" in lowered:
            return value.strip()
    return None


def raise_if_suspended(value: Any) -> None:
    message = suspension_message(value)
    if message:
        raise AccountSuspendedError(message)


def raise_if_authentication_failed(value: Any) -> None:
    message = authentication_message(value)
    if message:
        raise AuthenticationFailedError(message)


def load_cookies(path: str) -> list[dict[str, Any]]:
    """Load cookies exported by a browser and normalize nullable fields."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("The cookies file must contain a JSON list")

    cookies: list[dict[str, Any]] = []
    for item in data:
        cookie = dict(item)
        # Playwright accepts these values but not Chrome's `expirationDate` key.
        if "expirationDate" in cookie:
            cookie["expires"] = cookie.pop("expirationDate")
        cookie.pop("hostOnly", None)
        cookie.pop("session", None)
        cookie.pop("storeId", None)
        if cookie.get("sameSite") not in {"Strict", "Lax", "None"}:
            cookie.pop("sameSite", None)
        cookies.append(cookie)
    return cookies


def client_headers(token: str, locale: str = "ru") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "*/*",
        "Content-Type": "application/json",
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/",
        "X-Client-Bundle-Id": "com.deepseek.chat",
        "X-Client-Locale": locale,
        "X-Client-Platform": "web",
        "X-Client-Timezone-Offset": os.getenv("DEEPSEEK_TIMEZONE_OFFSET", "10800"),
        "X-Client-Version": os.getenv("DEEPSEEK_CLIENT_VERSION", "2.4.0"),
    }


class PowSolver:
    """Run DeepSeek's official WASM PoW solver locally."""

    def __init__(self) -> None:
        if Store is None:
            raise RuntimeError("Install wasmtime: python -m pip install wasmtime")
        cache = Path(os.getenv("DEEPSEEK_WASM", "sha3_wasm_bg.wasm"))
        if not cache.exists():
            print(f"downloading PoW solver to {cache}...", file=sys.stderr)
            try:
                with urllib.request.urlopen(
                    os.getenv("DEEPSEEK_WASM_URL", WASM_URL), timeout=30
                ) as response:
                    cache.write_bytes(response.read())
            except Exception as exc:
                raise RuntimeError(
                    f"Could not download {cache}. Download {WASM_URL} manually."
                ) from exc
        self.store = Store()
        instance = Instance(self.store, Module(self.store.engine, cache.read_bytes()), [])
        exports = instance.exports(self.store)
        self.memory = exports["memory"]
        self.solve_wasm = exports["wasm_solve"]
        self.add_stack = exports["__wbindgen_add_to_stack_pointer"]
        self.malloc = exports["__wbindgen_export_0"]
        self.free = exports["__wbindgen_export_2"]
        self.lock = threading.Lock()

    def solve(self, challenge: str, salt: str, expire_at: int, difficulty: float) -> int:
        with self.lock:
            allocations: list[tuple[int, int]] = []
            try:
                def write(value: str) -> tuple[int, int]:
                    raw = value.encode()
                    ptr = self.malloc(self.store, len(raw), 1)
                    memory = self.memory.data_ptr(self.store)
                    for index, byte in enumerate(raw):
                        memory[ptr + index] = byte
                    allocations.append((ptr, len(raw)))
                    return ptr, len(raw)

                stack = self.add_stack(self.store, -16)
                challenge_ptr, challenge_len = write(challenge)
                prefix_ptr, prefix_len = write(f"{salt}_{expire_at}_")
                self.solve_wasm(
                    self.store, stack, challenge_ptr, challenge_len,
                    prefix_ptr, prefix_len, float(difficulty)
                )
                memory = self.memory.data_ptr(self.store)
                status = bytes(memory[stack + index] for index in range(4))
                if int.from_bytes(status, "little", signed=True) == 0:
                    raise RuntimeError("DeepSeek PoW solver found no solution")
                nonce_bytes = bytes(memory[stack + index] for index in range(8, 16))
                return int(struct.unpack("<d", nonce_bytes)[0])
            finally:
                for ptr, length in allocations:
                    # Some current DeepSeek WASM builds trap in the
                    # wasm-bindgen free export after solving. The process
                    # performs only a few allocations per request, so a
                    # cleanup trap must not discard a valid PoW nonce.
                    try:
                        self.free(self.store, ptr, length, 1)
                    except Exception:
                        pass


async def create_pow_header(page: Page, headers: dict[str, str]) -> str:
    result = await page.evaluate(
        """async ({url, headers}) => {
            const response = await fetch(url, {
                method: 'POST', headers, body: JSON.stringify({target_path: '/api/v0/chat/completion'}),
                credentials: 'include'
            });
            return {status: response.status, body: await response.json()};
        }""",
        {"url": BASE_URL + POW_PATH, "headers": headers},
    )
    if result["status"] != 200:
        raise RuntimeError(f"PoW challenge HTTP {result['status']}: {result['body']}")
    try:
        return result["body"]["data"]["biz_data"]["challenge"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError(f"Unexpected PoW challenge response: {result['body']}") from exc


async def hif_headers(page: Page) -> dict[str, str]:
    """Fetch HIF signatures outside the page origin, avoiding browser CORS."""
    result: dict[str, str] = {}
    for url, header_name in ((HIF_LEIM_URL, "X-Hif-Leim"),):
        try:
            response = await page.context.request.get(url, timeout=10_000)
            if not response.ok:
                continue
            body = await response.json()
            value = body.get("data", {}).get("biz_data", {}).get("value")
            if value:
                result[header_name] = str(value)
        except Exception as exc:
            print(f"warning: could not fetch {header_name}: {exc}", file=sys.stderr)
    return result


async def pow_headers(page: Page, headers: dict[str, str]) -> dict[str, str]:
    challenge = await create_pow_header(page, headers)
    solver = PowSolver()
    answer = await asyncio.to_thread(
        solver.solve,
        challenge["challenge"],
        challenge["salt"],
        challenge["expire_at"],
        challenge["difficulty"],
    )
    payload = {
        "algorithm": "DeepSeekHashV1",
        "challenge": challenge["challenge"],
        "salt": challenge["salt"],
        "answer": answer,
        "signature": challenge["signature"],
        "target_path": challenge["target_path"],
    }
    result = dict(headers)
    result["X-DS-PoW-Response"] = base64.b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode()
    result.update(await hif_headers(page))
    return result


async def response_json(response: Any) -> dict[str, Any]:
    body = await response.json()
    raise_if_authentication_failed(body)
    raise_if_suspended(body)
    if not response.ok:
        raise RuntimeError(f"HTTP {response.status}: {body}")
    if body.get("code") not in (None, 0):
        raise RuntimeError(f"DeepSeek API error: {body}")
    return body


async def create_session(page: Page, headers: dict[str, str]) -> str:
    result = await page.evaluate(
        """async ({url, headers}) => {
            const response = await fetch(url, {
                method: 'POST', headers,
                body: JSON.stringify({target_path: '/api/v0/chat/completion'}),
                credentials: 'include'
            });
            return {status: response.status, body: await response.json()};
        }""",
        {"url": BASE_URL + CREATE_PATH, "headers": headers},
    )
    if result["status"] < 200 or result["status"] >= 300:
        raise_if_authentication_failed(result.get("body"))
        raise_if_suspended(result.get("body"))
        raise RuntimeError(f"HTTP {result['status']}: {result['body']}")
    body = result["body"]
    raise_if_authentication_failed(body)
    raise_if_suspended(body)
    if body.get("code") not in (None, 0):
        raise RuntimeError(f"DeepSeek API error: {body}")
    return body["data"]["biz_data"]["chat_session"]["id"]


async def capture_dynamic_headers(page: Page, prompt: str) -> dict[str, str]:
    """Ask the web UI to build a completion request and capture its headers."""
    captured: asyncio.Future[dict[str, str]] = asyncio.get_running_loop().create_future()

    async def on_route(route: Any) -> None:
        request = route.request
        headers = await request.all_headers()
        dynamic = {
            key: headers[key]
            for key in ("x-ds-pow-response", "x-hif-leim")
            if key in headers
        }
        if len(dynamic) == 2 and not captured.done():
            captured.set_result(dynamic)
        # Do not make the UI request a second answer. The headers are all we need.
        await route.abort()

    await page.route(f"**{COMPLETION_PATH}", on_route)
    try:
        textbox = page.locator("textarea:visible").last
        try:
            await textbox.wait_for(state="visible", timeout=10_000)
        except PlaywrightTimeoutError:
            textbox = page.get_by_role("textbox").last
            await textbox.wait_for(state="visible", timeout=10_000)
        await textbox.fill(prompt)
        await textbox.press("Enter")
        timeout = float(os.getenv("DEEPSEEK_HEADER_TIMEOUT", "20"))
        try:
            return await asyncio.wait_for(captured, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise RuntimeError(
                "DeepSeek UI did not produce a completion request. "
                "The login may be expired or the page layout may have changed."
            ) from exc
    finally:
        await page.unroute(f"**{COMPLETION_PATH}", on_route)


async def completion_events(
    page: Page,
    headers: dict[str, str],
    session_id: str,
    prompt: str,
    *,
    model_type: str = "default",
    thinking: bool,
    search: bool,
    parent_message_id: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    request_headers = dict(headers)
    # These are generated by the web client. They can be supplied when a
    # captured request is available; otherwise the server may issue a challenge.
    for name in ("DEEPSEEK_POW_RESPONSE", "DEEPSEEK_HIF_LEIM"):
        value = os.getenv(name)
        if value:
            request_headers["X-DS-Pow-Response" if name.endswith("POW_RESPONSE") else "X-Hif-Leim"] = value
    missing_dynamic = [
        name for name in ("X-DS-Pow-Response", "X-Hif-Leim") if name not in request_headers
    ]
    if missing_dynamic:
        print(
            "warning: completion request has no " + ", ".join(missing_dynamic) + "; "
            "the web API may reject its anti-abuse challenge",
            file=sys.stderr,
        )

    payload = {
        "chat_session_id": session_id,
        "parent_message_id": parent_message_id,
        "model_type": model_type,
        "prompt": prompt,
        "ref_file_ids": [],
        "stream": True,
        "thinking_enabled": thinking,
        "search_enabled": search,
        "action": None,
        "preempt": False,
    }
    print("sending completion request...", file=sys.stderr)
    if httpx is None:
        raise RuntimeError("Install httpx: python -m pip install httpx")

    cookie_header = "; ".join(
        f"{cookie['name']}={cookie['value']}" for cookie in await page.context.cookies(BASE_URL)
    )
    request_headers["Cookie"] = cookie_header
    request_headers["Accept"] = "text/event-stream"
    timeout = httpx.Timeout(
        float(os.getenv("DEEPSEEK_REQUEST_TIMEOUT", "30")),
        read=float(os.getenv("DEEPSEEK_STREAM_TIMEOUT", "120")),
    )
    buffer = ""
    async with httpx.AsyncClient(http2=True, timeout=timeout) as client:
        async with client.stream(
            "POST", BASE_URL + COMPLETION_PATH,
            headers=request_headers,
            json=payload,
        ) as response:
            if response.status_code < 200 or response.status_code >= 300:
                body = await response.aread()
                try:
                    parsed_body = json.loads(body.decode("utf-8", errors="replace"))
                    raise_if_authentication_failed(parsed_body)
                    raise_if_suspended(parsed_body)
                except json.JSONDecodeError:
                    body_text = body.decode("utf-8", errors="replace")
                    raise_if_authentication_failed(body_text)
                    raise_if_suspended(body_text)
                raise RuntimeError(f"Completion HTTP {response.status_code}: {body[:1000]!r}")
            decoder = codecs.getincrementaldecoder("utf-8")()
            async for chunk in response.aiter_bytes():
                buffer += decoder.decode(chunk)
                blocks = buffer.replace("\r\n", "\n").split("\n\n")
                buffer = blocks.pop()
                for event in parse_sse("\n\n".join(blocks)):
                    raise_if_authentication_failed(event)
                    raise_if_suspended(event)
                    yield event
            buffer += decoder.decode(b"", final=True)
    if buffer.strip():
        for event in parse_sse(buffer):
            raise_if_authentication_failed(event)
            raise_if_suspended(event)
            yield event
def parse_sse(text: str) -> list[dict[str, Any]]:
    """Parse DeepSeek's SSE blocks, including blocks without an event line."""
    events: list[dict[str, Any]] = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        if not block.strip():
            continue
        event: dict[str, Any] = {}
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event["event"] = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if data_lines:
            raw = "\n".join(data_lines)
            try:
                event["data"] = json.loads(raw)
            except json.JSONDecodeError:
                event["data"] = raw
        if event:
            events.append(event)
    return events


async def main(prompt: str, cookies_path: str, *, headed: bool) -> None:
    token = os.getenv("DEEPSEEK_BEARER_TOKEN")
    if not token:
        raise RuntimeError("DEEPSEEK_BEARER_TOKEN is not set")

    cookies = load_cookies(cookies_path)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=not headed)
        context: BrowserContext = await browser.new_context(
            user_agent=os.getenv(
                "DEEPSEEK_USER_AGENT",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
            )
        )
        await context.add_cookies(cookies)
        page = await context.new_page()
        page.set_default_timeout(30_000)
        try:
            # The web app can keep navigation open while loading analytics or
            # long-polling resources. The API calls do not require full load.
            await page.goto(BASE_URL + "/", wait_until="commit", timeout=15_000)
        except PlaywrightTimeoutError:
            print("warning: page navigation timed out; continuing with the API request", file=sys.stderr)

        headers = client_headers(token, os.getenv("DEEPSEEK_LOCALE", "ru"))
        print("requesting and solving DeepSeek PoW challenge...", file=sys.stderr)
        headers = await pow_headers(page, headers)
        session_id = await create_session(page, headers)
        print(f"session_id={session_id}", file=sys.stderr)
        # Completion needs a challenge for its own target path. Generate it
        # after session creation instead of trying to reuse the session PoW.
        headers = await pow_headers(page, client_headers(token, os.getenv("DEEPSEEK_LOCALE", "ru")))
        try:
            async for event in completion_events(
                page,
                headers,
                session_id,
                prompt,
                model_type="default",
                thinking=os.getenv("DEEPSEEK_THINKING", "true").lower() == "true",
                search=os.getenv("DEEPSEEK_SEARCH", "true").lower() == "true",
            ):
                print(json.dumps(event, ensure_ascii=False), flush=True)
        finally:
            await browser.close()


if __name__ == "__main__":
    load_dotenv()
    parser = argparse.ArgumentParser(description="Send a prompt to DeepSeek web chat")
    parser.add_argument("prompt", nargs="?", help="Prompt text")
    parser.add_argument("--cookies", default=os.getenv("DEEPSEEK_COOKIES", "cookies.json"))
    parser.add_argument("--headed", action="store_true", help="Show Chromium window")
    args = parser.parse_args()
    asyncio.run(main(args.prompt or input("Prompt: "), args.cookies, headed=args.headed))
