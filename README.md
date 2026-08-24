# DeepSeek Web to OpenCode

An experimental local OpenAI-compatible backend that connects OpenCode to the DeepSeek web chat service through a Playwright-controlled Chromium session.

The project is intended for personal, local development and research. It is not affiliated with, endorsed by, or supported by DeepSeek. It relies on private web endpoints that may change without notice.

## What It Provides

- A standalone Playwright client for sending prompts to DeepSeek Web.
- A local OpenAI-compatible API at `http://127.0.0.1:8787/v1`.
- Streaming Server-Sent Events (SSE) responses.
- OpenCode integration through `opencode.json`.
- Two local model aliases:
  - `deepseek-local/default`: DeepSeek web `model_type=default`, described here as **DeepSeek v4 Flash**.
  - `deepseek-local/extra`: DeepSeek web `model_type=expert`, described here as **DeepSeek v4 Pro**.
- Reasoning fragments for both model aliases when the upstream response contains THINK fragments.
- Experimental tool calling. OpenAI-compatible tool definitions are translated into DeepSeek DSML instructions, and DSML tool calls are converted back into OpenAI `tool_calls`.
- Automatic PoW challenge retrieval and local WASM solving.
- Automatic retrieval of the current HIF signature header.
- Explicit handling for invalid tokens and suspended accounts.

## Important Warning

This project uses private, undocumented DeepSeek Web endpoints and account credentials. Use it only with an account you control and only in accordance with DeepSeek's current Terms of Service and usage policies.

Do not use this project to bypass access restrictions, account suspensions, rate limits, CAPTCHAs, regional controls, or other security mechanisms. A valid token does not grant permission to evade platform safeguards.

The upstream service can suspend an account. The backend detects suspension messages and returns an explicit error, but it cannot restore access or override an upstream decision.

## Security

Never commit or publish any of the following:

- `.env`
- `cookies.json`
- Bearer tokens
- session cookies such as `ds_session_id`
- captured PoW or HIF headers
- logs containing request headers or credentials

These files are excluded by `.gitignore`. The token and cookies used during development must be considered compromised if they were pasted into a chat, issue, terminal recording, or public repository. Revoke or rotate them before publishing this project.

## Requirements

- Windows, macOS, or Linux
- Python 3.10 or newer
- A DeepSeek account with access to DeepSeek Web
- A current browser session exported as cookies
- A current DeepSeek Bearer token

## Installation

Create and activate a virtual environment:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Install Python dependencies and Chromium:

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
```

The PoW WASM module is downloaded automatically on first use. To download it explicitly:

```powershell
Invoke-WebRequest `
  -Uri "https://raw.githubusercontent.com/snake-aabb-wtf/deepseek-web2api-free/main/sha3_wasm_bg.wasm" `
  -OutFile "sha3_wasm_bg.wasm"
```

## Credentials

Copy the template:

```powershell
Copy-Item .env.example .env
```

Set a current Bearer token in `.env`:

```dotenv
DEEPSEEK_BEARER_TOKEN=replace_with_a_current_token
DEEPSEEK_COOKIES=cookies.json
DEEPSEEK_LOCALE=ru
DEEPSEEK_TIMEZONE_OFFSET=10800
DEEPSEEK_CLIENT_VERSION=2.4.0
DEEPSEEK_THINKING=true
DEEPSEEK_SEARCH=false
```

Save browser cookies in `cookies.json` as a JSON array in the format accepted by Playwright. Do not paste real credentials into source files.

Credentials expire. If the service returns `Authorization Failed (invalid token)` or error code `40003`, refresh the token and cookies from the current browser session.

## Standalone Client

Send a prompt directly to DeepSeek Web:

```powershell
python deepseek_playwright.py "What can you do?"
```

Show the Chromium window while debugging:

```powershell
python deepseek_playwright.py --headed "Hello"
```

The client prints parsed DeepSeek SSE events to stdout. PoW and HIF headers are generated at runtime; they should not be copied into `.env` or committed.

## Local API Backend

Start the OpenAI-compatible backend:

```powershell
python -m uvicorn deepseek_backend:app --host 127.0.0.1 --port 8787
```

The backend exposes:

```text
GET  http://127.0.0.1:8787/v1/models
POST http://127.0.0.1:8787/v1/chat/completions
```

Check the available model aliases:

```powershell
Invoke-RestMethod http://127.0.0.1:8787/v1/models
```

Test a non-streaming request:

```powershell
$body = @{
  model = "default"
  messages = @(@{ role = "user"; content = "Reply with exactly: OK" })
  stream = $false
} | ConvertTo-Json -Depth 8

Invoke-RestMethod `
  -Uri "http://127.0.0.1:8787/v1/chat/completions" `
  -Method Post `
  -ContentType "application/json" `
  -Body $body
```

## OpenCode

The repository includes an `opencode.json` configuration for the local provider. Start the backend first, then restart OpenCode so it reloads the project configuration.

Available model identifiers:

```text
deepseek-local/default
deepseek-local/extra
```

The local provider uses the OpenAI-compatible base URL:

```text
http://127.0.0.1:8787/v1
```

The configured API key is a local placeholder only. The backend authenticates upstream using `DEEPSEEK_BEARER_TOKEN` and `cookies.json`.

## Tool Calling

OpenCode sends tool definitions using the OpenAI-compatible `tools` field. The backend:

1. Adds the available tools to the prompt using DeepSeek's DSML format.
2. Parses `<tool_calls>`, `<invoke>`, and `<parameter>` variants.
3. Supports CDATA values and Windows paths.
4. Converts calls back into OpenAI-compatible `tool_calls`.
5. Streams tool arguments in indexed chunks so OpenCode can reconstruct long arguments reliably.

Tool calling is experimental. DeepSeek may return malformed markup, choose not to call a tool, or produce a response that requires parser updates. The backend does not execute tools itself; OpenCode remains responsible for tool execution.

## Error Handling

The backend distinguishes several upstream failures:

- `401 Unauthorized`: the Bearer token is missing, expired, or invalid.
- `403 Forbidden`: the upstream reports that the account is suspended.
- `502 Bad Gateway`: an upstream protocol, network, PoW, or unexpected response error occurred.

Examples of recognized upstream messages include:

```text
Authorization Failed (invalid token)
Due to violation of user policies, your account has been suspended until ...
```

The local server does not bypass or resolve these conditions. Refresh credentials only after confirming that the account is permitted to use the service.

## Configuration

Useful environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `DEEPSEEK_BEARER_TOKEN` | unset | Current upstream Bearer token |
| `DEEPSEEK_COOKIES` | `cookies.json` | Playwright cookie export path |
| `DEEPSEEK_WASM` | `sha3_wasm_bg.wasm` | Local PoW WASM path |
| `DEEPSEEK_REQUEST_TIMEOUT` | `30` | Completion connection timeout in seconds |
| `DEEPSEEK_STREAM_TIMEOUT` | `120` | Maximum wait between SSE chunks in seconds |
| `OPENCODE_HOST` | `127.0.0.1` | Local backend bind address |
| `OPENCODE_PORT` | `8787` | Local backend port |

## Development Checks

Run the syntax check:

```powershell
python -m py_compile deepseek_playwright.py deepseek_backend.py
```

Check the OpenCode configuration is valid JSON:

```powershell
python -c "import json; json.load(open('opencode.json', encoding='utf-8')); print('opencode.json: ok')"
```

## Project Files

| File | Purpose |
| --- | --- |
| `deepseek_playwright.py` | Playwright session, PoW, HIF, session creation, SSE client, and upstream error detection |
| `deepseek_backend.py` | FastAPI OpenAI-compatible local backend and DSML tool adapter |
| `opencode.json` | OpenCode provider and model configuration |
| `requirements.txt` | Python dependencies |
| `.env.example` | Safe configuration template |
| `.gitignore` | Credential and local-artifact exclusions |

## License and Responsibility

No license is currently declared. Until a license is added, assume that copying, redistribution, and commercial use are not granted.

This is an unofficial research project. You are responsible for your credentials, account activity, network traffic, compliance obligations, and any consequences of using private upstream endpoints.
