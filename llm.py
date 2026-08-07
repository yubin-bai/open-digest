"""One LLM entry point, routed through OpenRouter (OpenAI-compatible protocol).

One API key gets you every model — Claude, GPT, Gemini, DeepSeek, Llama, Qwen —
by changing a string in the config. Model names belong in config, never in code.

    import llm
    r = llm.chat("deepseek/deepseek-v4-pro", system="...", user="...")
    r["text"], r["prompt_tokens"], r["completion_tokens"], r["latency_s"]
"""

from __future__ import annotations

import os
import time

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")


def _client() -> OpenAI:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError(
            "OPENROUTER_API_KEY is not set. Put it in .env or export it. "
            "Get one at https://openrouter.ai/keys")
    return OpenAI(base_url=BASE_URL, api_key=key)


def chat(model: str, system: str, user: str, max_tokens: int = 2000,
         json_mode: bool = True, json_schema: dict = None,
         temperature: float = None) -> dict:
    """Single-turn completion.

    Structure constraints degrade gracefully: json_schema (strongest) ->
    json_object -> unconstrained. Not every model on OpenRouter supports every
    tier, so a rejection drops one level and retries. The real guarantee is the
    caller's exit validation, not the provider's promise.
    """
    client = _client()
    kwargs = dict(model=model, max_tokens=max_tokens, messages=[
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ])
    if temperature is not None:
        kwargs["temperature"] = temperature

    formats = []
    if json_schema:
        formats.append({"type": "json_schema", "json_schema": json_schema})
    if json_mode:
        formats.append({"type": "json_object"})
    formats.append(None)

    t0 = time.monotonic()
    resp, last_exc = None, None
    for fmt in formats:
        if fmt is None:
            kwargs.pop("response_format", None)
        else:
            kwargs["response_format"] = fmt
        try:
            resp = client.chat.completions.create(**kwargs)
            break
        except Exception as e:  # noqa: BLE001 - try the next constraint tier
            last_exc = e
    if resp is None:
        raise last_exc

    usage = getattr(resp, "usage", None)
    return {
        "text": resp.choices[0].message.content or "",
        "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
        "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
        "latency_s": round(time.monotonic() - t0, 2),
        "model": model,
    }
