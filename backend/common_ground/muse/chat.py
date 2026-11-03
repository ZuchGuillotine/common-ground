"""Async client for Muse chat completions (OpenAI-compatible Chat Completions protocol).

Wire shape: https://dev.meta.ai/docs/protocols/chat-completions
Structured output: https://dev.meta.ai/docs/structured-output
Errors/retries: https://dev.meta.ai/docs/error-handling
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
from typing import Any, Self

import httpx

from .base import Message, MuseError, UsageMeter
from .models import CHAT_COMPLETIONS_PATH, DEFAULT_BASE_URL, DEFAULT_CHAT_MODEL, MODELS_PATH, price_for

RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 3

# VERIFIED LIVE 2026-11-03: Muse Spark's reasoning tokens are billed as output tokens AND count against
# `max_completion_tokens`; when the budget runs out mid-reasoning the response has finish_reason="length"
# and `message.content: null` (no partial JSON). Even reasoning_effort="minimal" spent ~60 tokens on a
# one-word question ("low" ~200, "medium" ~330); on the interpreter prompt "low" spent 1-2.5k tokens
# (p50 latency 13.7 s) and once exhausted a 2,560-token budget. So: `max_tokens` is the *visible* output
# budget with `REASONING_HEADROOM_TOKENS` added on top, the default effort is "minimal" (this is a
# latency-bound coaching loop), and an exhausted budget is retried once with "minimal" effort and double
# the budget before giving up. `reasoning_effort: "none"` is rejected by the API (HTTP 400).
DEFAULT_REASONING_EFFORT = os.environ.get("LLAMA_REASONING_EFFORT", "minimal")
REASONING_HEADROOM_TOKENS = int(os.environ.get("LLAMA_REASONING_HEADROOM", "1536"))
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class MuseChatError(MuseError):
    budget_exhausted = False   # True when reasoning consumed max_completion_tokens before any visible text

    def __init__(self, message: str, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status, self.body = status, body


def api_key_from_env() -> str | None:
    return os.environ.get("LLAMA_API_KEY") or os.environ.get("MODEL_API_KEY")


def base_url_from_env() -> str:
    return (os.environ.get("LLAMA_API_BASE_URL") or os.environ.get("MODEL_API_BASE_URL")
            or DEFAULT_BASE_URL).rstrip("/")


def extract_json(text: str) -> dict:
    """Tolerant JSON extraction: plain JSON, fenced JSON, or the first balanced {...} object."""
    candidates = [text.strip()] + [m.strip() for m in _FENCE.findall(text)]
    start = text.find("{")
    if start != -1:
        depth, in_str, esc = 0, False, False
        for i, ch in enumerate(text[start:], start):
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str, esc = True, False
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start:i + 1])
                    break
    for c in candidates:
        try:
            obj = json.loads(c)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    raise MuseChatError(f"no JSON object found in model output: {text[:200]!r}")


class MuseChat:
    """`complete()` returns text; `complete_json()` returns a dict (JSON-schema structured output)."""

    def __init__(self, api_key: str | None = None, base_url: str | None = None, *,
                 model: str = DEFAULT_CHAT_MODEL, timeout: float = 60.0,
                 reasoning_effort: str | None = DEFAULT_REASONING_EFFORT,
                 client: httpx.AsyncClient | None = None):
        self.api_key = api_key or api_key_from_env()
        if not self.api_key:
            raise MuseChatError("LLAMA_API_KEY (or MODEL_API_KEY) is not set")
        self.base_url = (base_url or base_url_from_env()).rstrip("/")
        self.model = model
        self.reasoning_effort = reasoning_effort   # minimal|low|medium|high|xhigh; None = server default
        self.usage = UsageMeter()
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ---------------------------------------------------------------- public
    async def list_models(self) -> list[str]:
        data = await self._request("GET", MODELS_PATH)
        return [m.get("id", "?") for m in data.get("data", data.get("models", []))]

    async def complete(self, messages: list[Message], *, model: str | None = None,
                       json_schema: dict | None = None, max_tokens: int = 1024,
                       temperature: float | None = None) -> str:
        try:
            return await self._complete(messages, model=model, json_schema=json_schema, max_tokens=max_tokens,
                                        temperature=temperature, reasoning_effort=self.reasoning_effort)
        except MuseChatError as e:
            if not e.budget_exhausted:
                raise
        # reasoning ate the whole budget: one more try, cheapest reasoning and twice the room
        return await self._complete(messages, model=model, json_schema=json_schema, max_tokens=max_tokens,
                                    temperature=temperature, reasoning_effort="minimal",
                                    headroom=2 * REASONING_HEADROOM_TOKENS + max_tokens)

    async def _complete(self, messages: list[Message], *, model: str | None, json_schema: dict | None,
                        max_tokens: int, temperature: float | None, reasoning_effort: str | None,
                        headroom: int = REASONING_HEADROOM_TOKENS) -> str:
        body: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            # `max_tokens` is the visible-output budget; reasoning tokens share the cap, so add headroom.
            "max_completion_tokens": max_tokens + headroom,
        }
        if reasoning_effort:
            body["reasoning_effort"] = reasoning_effort
        if temperature is not None:               # docs: Muse Spark is tuned for the default (1.0)
            body["temperature"] = temperature
        if json_schema is not None:
            body["response_format"] = {   # VERIFIED: `name` is required (400 without it); strict defaults false
                "type": "json_schema",
                "json_schema": {"name": json_schema.get("title", "Output"), "schema": json_schema},
            }
        data = await self._request("POST", CHAT_COMPLETIONS_PATH, body)
        usage = data.get("usage") or {}
        self.usage.add(int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)),
                       price_for(body["model"]))
        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise MuseChatError(f"unexpected response shape: {data}", body=data) from e
        if content is None and choice.get("finish_reason") == "length":
            details = usage.get("completion_tokens_details") or {}
            err = MuseChatError(
                f"output budget exhausted before any visible text (max_completion_tokens="
                f"{body['max_completion_tokens']}, reasoning_effort={reasoning_effort}, "
                f"reasoning_tokens={details.get('reasoning_tokens')}); raise max_tokens or lower "
                f"reasoning_effort", body=data)
            err.budget_exhausted = True
            raise err
        if isinstance(content, list):   # multi-part content; keep text parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        return content or ""

    async def complete_json(self, messages: list[Message], *, model: str | None = None,
                            json_schema: dict | None = None, max_tokens: int = 1024,
                            temperature: float | None = None) -> dict:
        if json_schema is None:   # no schema: ask for JSON in-band and parse tolerantly
            messages = [{"role": "system", "content": "Reply with a single JSON object and nothing else."},
                        *messages]
        text = await self.complete(messages, model=model, json_schema=json_schema,
                                   max_tokens=max_tokens, temperature=temperature)
        return extract_json(text)

    # -------------------------------------------------------------- internal
    async def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        url = self.base_url + path
        last: MuseChatError | None = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                r = await self._client.request(method, url, headers=headers, json=body)
            except httpx.HTTPError as e:
                last = MuseChatError(f"network error: {e}")
            else:
                if r.status_code < 400:
                    return r.json()
                last = MuseChatError(f"HTTP {r.status_code} from {path}: {_err_message(r)}",
                                     status=r.status_code, body=_safe_json(r))
                if r.status_code not in RETRY_STATUSES:
                    raise last
            if attempt < MAX_RETRIES:
                await asyncio.sleep(_backoff(attempt, last))
        assert last is not None
        raise last


def _backoff(attempt: int, err: MuseChatError | None) -> float:
    # docs: start 0.5-1 s, double each retry, add jitter, honour Retry-After (capped here at 30 s)
    delay = 0.5 * (2 ** attempt) + random.random() * 0.5
    retry_after = (err.body or {}).get("_retry_after") if err and isinstance(err.body, dict) else None
    if retry_after:
        delay = max(delay, min(float(retry_after), 30.0))
    return delay


def _safe_json(r: httpx.Response) -> dict:
    try:
        data = r.json()
    except ValueError:
        data = {"raw": r.text[:500]}
    if not isinstance(data, dict):
        data = {"raw": data}
    if r.headers.get("Retry-After", "").replace(".", "", 1).isdigit():
        data["_retry_after"] = r.headers["Retry-After"]
    return data


def _err_message(r: httpx.Response) -> str:
    data = _safe_json(r)
    err = data.get("error")
    return err.get("message", str(err)) if isinstance(err, dict) else str(data)[:300]
