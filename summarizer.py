"""Turn raw fetched items into a digest, one LLM call per topic.

The old version hardcoded four sections into the prompt and the JSON schema.
Here both are *built from your config*: whatever topics you declare, the schema
and prompt follow. Adding a topic changes no code.

Per-topic calls (rather than one giant call) buy three things: each topic gets a
prompt written for it, one bad topic can't void the whole digest, and material
for 10 topics won't blow the context window.

Structure is enforced in three layers, strongest first:
  1. OpenRouter structured outputs (json_schema) — protocol level
  2. prompt states the exact JSON shape — instruction level
  3. ``_parse_validate`` at the exit — the only layer fully under our control
Invalid output is retried once, then the topic is skipped with a warning.
"""

from __future__ import annotations

import json
import re

import json_repair

import llm

MAX_MATERIAL_CHARS = 90_000

SYSTEM_TEMPLATE = """You are the editor of a personal daily digest.

Section: "{title}"
What the reader wants from this section: {focus}

From the raw material below, select at most {max_items} items worth reading and
write the section. Reply with a JSON object only — no markdown fence, no prose
before or after.

Exact shape (field names must match):
{{
  "items": [ {{ "title": "...", "summary": "...", "url": "..." }} ]
}}

Rules:
- Rank by relevance to the section's stated focus; drop anything off-topic.
  Returning 2 strong items beats padding to {max_items} with filler.
- {summary_style}
- Summarize only what the material says. Do not add facts, opinions, or advice
  that is not in the source text.
- Every `url` must be copied verbatim from the material. Never invent a URL.
- Write in: {language}
"""

ITEM_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "url": {"type": "string"},
    },
    "required": ["title", "summary", "url"],
}

SECTION_SCHEMA = {
    "name": "digest_section",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {"items": {"type": "array", "items": ITEM_SCHEMA}},
        "required": ["items"],
    },
}

HEADLINE_SCHEMA = {
    "name": "headline",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {"headline": {"type": "string"}},
        "required": ["headline"],
    },
}

DEFAULT_STYLE = ("Each summary is 2-4 sentences: what the piece is about, then its "
                 "core method or finding, then the result or takeaway.")


def summarize_topic(topic: dict, items: list, model: str,
                    language: str = "English", max_tokens: int = 2000) -> list:
    """Summarize one topic's raw items. Returns ``[]`` rather than raising."""
    if not items:
        return []
    system = SYSTEM_TEMPLATE.format(
        title=topic.get("title", topic.get("id", "Section")),
        focus=topic.get("focus") or topic.get("title") or "anything noteworthy",
        max_items=topic.get("max_items", 5),
        summary_style=topic.get("summary_style") or DEFAULT_STYLE,
        language=language,
    )
    material = json.dumps(items, ensure_ascii=False, indent=1)[:MAX_MATERIAL_CHARS]
    user = f"Raw material:\n{material}"

    valid_urls = {i.get("url", "") for i in items if i.get("url")}
    last_err = None
    for _attempt in range(2):
        try:
            r = llm.chat(model, system, user, max_tokens=max_tokens,
                         json_mode=True, json_schema=SECTION_SCHEMA)
        except Exception as e:  # noqa: BLE001 - network/provider errors
            last_err = e
            print(f"   [warn] LLM call failed for '{topic.get('id')}': {e}")
            continue
        try:
            return _parse_validate(r["text"], valid_urls)
        except ValueError as e:
            last_err = e
            print(f"   [warn] invalid output for '{topic.get('id')}' ({e}), retrying")
    print(f"   [warn] giving up on topic '{topic.get('id')}': {last_err}")
    return []


def make_headline(sections: dict, model: str, language: str = "English") -> str:
    """One line over the whole digest. Best-effort: falls back to the top title."""
    titles = [i["title"] for items in sections.values() for i in items][:25]
    if not titles:
        return ""
    system = (
        "You write the one-line overview at the top of a daily digest.\n"
        'Reply with JSON only: {"headline": "..."}\n'
        "One factual sentence naming the single most significant item of the day. "
        "No hype, no second-guessing, no advice.\n"
        f"Write in: {language}"
    )
    try:
        r = llm.chat(model, system, "Today's headlines:\n- " + "\n- ".join(titles),
                     max_tokens=300, json_mode=True, json_schema=HEADLINE_SCHEMA)
        d = _loads(r["text"])
        return str(d.get("headline", "")).strip() or titles[0]
    except Exception as e:  # noqa: BLE001
        print(f"   [warn] headline generation failed ({e}), using top item")
        return titles[0]


def _loads(text: str) -> dict:
    """Extract a JSON object from model output, repairing near-JSON if needed."""
    t = re.sub(r"\s*```$", "", re.sub(r"^```(?:json)?\s*", "", text.strip()))
    m = re.search(r"\{.*\}", t, re.S)  # tolerate chatter around the object
    if not m:
        raise ValueError("no JSON object in output")
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        try:
            d = json_repair.loads(m.group(0))  # unescaped quotes, trailing commas
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"unparseable JSON: {e}") from e
    if not isinstance(d, dict):
        raise ValueError("top level is not an object")
    return d


def _parse_validate(text: str, valid_urls: set) -> list:
    """Exit gate. Anything unrenderable is dropped; total wipeout raises."""
    d = _loads(text)
    raw = d.get("items", [])
    if isinstance(raw, str):  # models have really shipped double-encoded arrays
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"'items' is an unparseable string: {e}") from e
    if not isinstance(raw, list):
        raise ValueError("'items' is not an array")

    out, hallucinated = [], 0
    for it in raw:
        if not isinstance(it, dict):
            continue
        title = str(it.get("title", "")).strip()
        url = str(it.get("url", "")).strip()
        summary = str(it.get("summary") or it.get("note") or "").strip()
        if not title or not url or not summary:
            continue
        if valid_urls and url not in valid_urls:
            hallucinated += 1  # URL not in the material == fabricated
            continue
        out.append({"title": title, "summary": summary, "url": url})
    if hallucinated:
        print(f"   [warn] dropped {hallucinated} item(s) with URLs not in the material")
    if not out:
        raise ValueError("no usable items after validation")
    return out
