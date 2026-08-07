"""Interactive setup — describe your topics in a sentence, get a digest.yaml.

    python wizard.py                 # build a config
    python wizard.py -o work.yaml    # write somewhere else
    python wizard.py --manual        # skip the AI step, answer everything yourself

With an OpenRouter key present, each topic is one question: say what you want in
plain language and the wizard works out the search queries, sources and prompt.
Without a key it falls back to a short manual flow.

digest.yaml is plain YAML — editing it by hand afterwards is always fine.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys

import yaml

C = {"b": "\033[1m", "d": "\033[2m", "g": "\033[32m", "c": "\033[36m",
     "y": "\033[33m", "0": "\033[0m"}
if not sys.stdout.isatty():
    C = dict.fromkeys(C, "")

# Full-width punctuation from Chinese/Japanese IMEs, normalized to ASCII so
# "1，2" works as well as "1,2". Not doing this made the first version unusable.
SEPARATORS = str.maketrans({"，": ",", "、": ",", "；": ",", ";": ",",
                            "　": " ", "：": ":"})


def say(s=""):
    print(s)


def rule(title=""):
    say(f"\n{C['c']}{'─' * 62}{C['0']}")
    if title:
        say(f"{C['b']}{title}{C['0']}")


def ask(prompt, default=None, required=False):
    suffix = f" {C['d']}[{default}]{C['0']}" if default else ""
    while True:
        try:
            v = input(f"{prompt}{suffix}: ").strip()
        except EOFError:
            return default or ""
        if v:
            return v
        if default is not None or not required:
            return default or ""
        say(f"  {C['y']}This one can't be blank.{C['0']}")


def ask_list(prompt, default=None):
    raw = ask(prompt, ", ".join(default) if default else None)
    return [x.strip() for x in raw.translate(SEPARATORS).split(",") if x.strip()]


def ask_int(prompt, default):
    while True:
        v = ask(prompt, str(default))
        if str(v).isdigit():
            return int(v)
        say(f"  {C['y']}Enter a number.{C['0']}")


def ask_yn(prompt, default=True):
    d = "Y/n" if default else "y/N"
    try:
        v = input(f"{prompt} {C['d']}[{d}]{C['0']}: ").strip().lower()
    except EOFError:
        return default
    return default if not v else v[0] in "yY"


def ask_choice(prompt, options, default=1):
    """options: list of (value, label, hint). Returns one value."""
    say(f"\n{prompt}")
    for i, (_, label, hint) in enumerate(options, 1):
        say(f"  {C['b']}{i}{C['0']}. {label}  {C['d']}{hint}{C['0']}")
    while True:
        v = ask("  >", str(default)).translate(SEPARATORS).strip()
        if v.isdigit() and 1 <= int(v) <= len(options):
            return options[int(v) - 1][0]
        say(f"  {C['y']}Enter 1-{len(options)}.{C['0']}")


def ask_multi(prompt, options, default="1"):
    """Multi-select: '1,3' / '1，3' / '1 3' all work. Returns list of values."""
    say(f"\n{prompt}")
    for i, (_, label, hint) in enumerate(options, 1):
        say(f"  {C['b']}{i}{C['0']}. {label}  {C['d']}{hint}{C['0']}")
    while True:
        raw = ask(f"  {C['d']}(one or more, e.g. 1,3){C['0']} >", default)
        picks = [p for p in re.split(r"[,\s]+", raw.translate(SEPARATORS)) if p]
        if picks and all(p.isdigit() and 1 <= int(p) <= len(options) for p in picks):
            seen, out = set(), []
            for p in picks:
                v = options[int(p) - 1][0]
                if v not in seen:
                    seen.add(v)
                    out.append(v)
            return out
        say(f"  {C['y']}Enter one or more numbers, 1-{len(options)}.{C['0']}")


def slug(s: str) -> str:
    out = "".join(ch if ch.isalnum() else "_" for ch in s.lower()).strip("_")
    return re.sub(r"_+", "_", out) or "topic"


# --------------------------------------------------------------------------
# AI-assisted topic building
# --------------------------------------------------------------------------
TOPIC_SCHEMA = {
    "name": "topic",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "title": {"type": "string"},
            "focus": {"type": "string"},
            "queries": {"type": "array", "items": {"type": "string"}},
            "subreddits": {"type": "array", "items": {"type": "string"}},
            "arxiv_categories": {"type": "array", "items": {"type": "string"}},
            "hn_keywords": {"type": "array", "items": {"type": "string"}},
            "tickers": {"type": "array", "items": {"type": "string"}},
            "max_items": {"type": "integer"},
        },
        "required": ["title", "focus", "queries", "subreddits",
                     "arxiv_categories", "hn_keywords", "tickers", "max_items"],
    },
}

PLANNER_SYSTEM = """You configure one section of a personal news digest.

The user describes what they want in one sentence. Turn it into a plan.
Reply with JSON only.

  title       short section heading, 1-4 words, in the user's own language
  focus       a precise instruction for the summarizer: what to include AND what
              to exclude. Write it in English. Sharpen what the user said —
              if they say "stock trends" infer they want price moves, earnings
              and analyst outlook, not company gossip.
  queries     3-6 web search queries that would actually surface this material.
              These go to Google News, so use terms that appear in headlines.
              For a non-English topic, write queries in that language.
              Never turn an abstract goal into a query: someone asking about
              "long-term holding" wants queries about the specific companies and
              their earnings, not the literal phrase "long term holding".
  subreddits  relevant subreddit names, no r/ prefix. [] if none fit.
  arxiv_categories  arXiv category codes, only for academic research topics. []
              otherwise.
  hn_keywords search terms for Hacker News, only for software/tech/startup
              topics. [] otherwise.
  tickers     Yahoo Finance symbols if the user named specific companies,
              funds or crypto. Use the correct exchange suffix: AAPL, 600150.SS
              (Shanghai), 0700.HK (Hong Kong), 7203.T (Tokyo), BTC-USD. []
              if the topic is not about markets.
  max_items   how many items belong in this section, 3-8.

Choose sources by fit, not by filling every field. A cooking topic gets queries
and maybe a subreddit; empty arrays everywhere else are the right answer."""


def plan_topic(description: str, language: str, model: str):
    """Ask the model to turn one sentence into a topic config. None on failure."""
    import llm
    r = llm.chat(model, PLANNER_SYSTEM,
                 f"The digest is written in: {language}\n"
                 f"The user wants a section about: {description}",
                 max_tokens=900, json_mode=True, json_schema=TOPIC_SCHEMA)
    text = re.sub(r"\s*```$", "", re.sub(r"^```(?:json)?\s*", "", r["text"].strip()))
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    d = json.loads(m.group(0))
    return d if isinstance(d, dict) else None


def topic_from_plan(plan: dict, description: str) -> list:
    """Plan -> one or two topic blocks (a markets topic also gets a ticker widget)."""
    title = (plan.get("title") or description)[:40]
    queries = [q for q in plan.get("queries", []) if q] or [description]
    specs = [{"type": "news", "queries": queries, "per_query": 6, "window_days": 3}]
    if plan.get("subreddits"):
        specs.append({"type": "reddit", "subs": plan["subreddits"][:3], "limit": 8})
    if plan.get("hn_keywords"):
        specs.append({"type": "hackernews", "keywords": plan["hn_keywords"][:5],
                      "min_score": 50})
    if plan.get("arxiv_categories"):
        specs.append({"type": "arxiv", "categories": plan["arxiv_categories"][:3],
                      "max_results": 15})

    out = [{"id": slug(title), "title": title,
            "focus": (plan.get("focus") or description).strip(),
            "max_items": min(max(int(plan.get("max_items") or 5), 2), 10),
            "sources": specs}]
    if plan.get("tickers"):
        out.append({"id": slug(title) + "_quotes", "title": f"{title} · Quotes",
                    "kind": "tickers", "symbols": plan["tickers"][:8],
                    "news_per_ticker": 2})
    return out


def describe(topics: list) -> str:
    """One-line summary of what was planned, so the user can sanity-check it."""
    bits = []
    for t in topics:
        if t.get("kind") == "tickers":
            bits.append(f"quotes for {', '.join(t['symbols'])}")
        else:
            for s in t["sources"]:
                if s["type"] == "news":
                    bits.append("news: " + ", ".join(s["queries"][:3])
                                + ("…" if len(s["queries"]) > 3 else ""))
                elif s["type"] == "reddit":
                    bits.append("r/" + ", r/".join(s["subs"]))
                elif s["type"] == "hackernews":
                    bits.append("Hacker News")
                elif s["type"] == "arxiv":
                    bits.append("arXiv " + ", ".join(s["categories"]))
    return " · ".join(bits)


# --------------------------------------------------------------------------
# Manual fallback — short on purpose
# --------------------------------------------------------------------------
SOURCE_MENU = [
    ("news", "Web news", "any keyword becomes a feed — the safe default"),
    ("reddit", "Reddit", "you'll be asked for subreddit names"),
    ("hackernews", "Hacker News", "software and startups"),
    ("arxiv", "arXiv", "academic preprints"),
    ("rss", "RSS feeds", "you'll be asked for feed URLs"),
]


def manual_topic(n: int) -> list:
    rule(f"Topic {n}")
    title = ask("  Section title", required=True)
    say(f"  {C['d']}What should it cover, and what should it skip?{C['0']}")
    focus = ask("  Focus", title)
    say(f"  {C['d']}Words that would appear in a headline about this.{C['0']}")
    queries = ask_list("  Search terms", [title])

    specs = []
    for kind in ask_multi("  Where should it look?", SOURCE_MENU, "1"):
        if kind == "news":
            specs.append({"type": "news", "queries": list(queries),
                          "per_query": 6, "window_days": 3})
        elif kind == "reddit":
            subs = ask_list("    Subreddits (blank to skip)")
            if subs:
                specs.append({"type": "reddit", "subs": subs, "limit": 8})
        elif kind == "hackernews":
            specs.append({"type": "hackernews", "keywords": list(queries),
                          "min_score": 50})
        elif kind == "arxiv":
            cats = ask_list("    arXiv categories, e.g. cs.LG (blank to skip)")
            if cats:
                specs.append({"type": "arxiv", "categories": cats, "max_results": 15})
        elif kind == "rss":
            feeds = ask_list("    Feed URLs (blank to skip)")
            if feeds:
                specs.append({"type": "rss", "feeds": feeds, "per_feed": 8})
    if not specs:  # everything was skipped — don't leave a dead topic
        specs = [{"type": "news", "queries": list(queries),
                  "per_query": 6, "window_days": 3}]
    return [{"id": slug(title), "title": title, "focus": focus,
             "max_items": 5, "sources": specs}]


# --------------------------------------------------------------------------
def get_key() -> str:
    """Find an OpenRouter key, or offer to save one. '' means manual mode."""
    from dotenv import load_dotenv
    load_dotenv()
    if os.environ.get("OPENROUTER_API_KEY"):
        return os.environ["OPENROUTER_API_KEY"]

    say(f"\n{C['d']}open-digest needs an OpenRouter key to write summaries.")
    say(f"Get one at {C['c']}https://openrouter.ai/keys{C['0']}{C['d']} — "
        f"a year of digests costs a few dollars.")
    say(f"Paste it now and setup gets much shorter, or press Enter to "
        f"configure by hand.{C['0']}")
    key = ask("  OpenRouter key")
    if not key:
        return ""
    env = pathlib.Path(".env")
    body = env.read_text(encoding="utf-8") if env.exists() else ""
    if "OPENROUTER_API_KEY=" in body:
        body = re.sub(r"OPENROUTER_API_KEY=.*", f"OPENROUTER_API_KEY={key}", body)
    else:
        body += f"\nOPENROUTER_API_KEY={key}\n"
    env.write_text(body.lstrip("\n"), encoding="utf-8")
    os.environ["OPENROUTER_API_KEY"] = key
    say(f"  {C['g']}Saved to .env{C['0']}")
    return key


MODEL_MENU = [
    ("deepseek/deepseek-v4-pro", "DeepSeek V4 Pro", "~$4/year — recommended"),
    ("anthropic/claude-fable-5", "Claude Fable 5", "best quality, ~$150/year"),
    ("openai/gpt-5-mini", "GPT-5 mini", "in between"),
]

EXAMPLES = [
    "苹果和中国船舶的股价走势、财报和分析师观点",
    "AI model releases and research results, skip funding news",
    "Sourdough technique — hydration and fermentation, no equipment reviews",
]


def main(out_path="digest.yaml", manual=False):
    say(f"\n{C['b']}open-digest setup{C['0']}")
    say(f"{C['d']}Enter accepts the bracketed default. Ctrl-C quits.{C['0']}")

    out = pathlib.Path(out_path)
    if out.exists() and not ask_yn(f"\n{out} exists. Overwrite?", False):
        say("Nothing written.")
        return

    key = "" if manual else get_key()
    model = "deepseek/deepseek-v4-pro"

    rule("Basics")
    title = ask("  Digest title", "My Daily Digest")
    say(f"  {C['d']}Any instruction works, e.g. "
        f"\"Chinese, keep technical terms in English\".{C['0']}")
    language = ask("  Write the digest in", "English")

    rule("Topics")
    topics = []
    if key:
        say("Describe each section in one sentence — what you want, and what to skip.")
        say(f"{C['d']}For example:{C['0']}")
        for e in EXAMPLES:
            say(f"{C['d']}  {e}{C['0']}")
        while True:
            prompt = (f"\n  Topic {len(topics) and '' or '1'}"
                      if not topics else
                      f"\n  Another topic {C['d']}(Enter to finish){C['0']}")
            desc = ask(prompt.strip())
            if not desc:
                if topics:
                    break
                say(f"  {C['y']}Need at least one.{C['0']}")
                continue
            say(f"  {C['d']}working…{C['0']}")
            try:
                plan = plan_topic(desc, language, model)
            except Exception as e:  # noqa: BLE001
                say(f"  {C['y']}Couldn't reach the model ({e}).{C['0']}")
                say(f"  {C['d']}Falling back to manual entry.{C['0']}")
                key = ""
                break
            if not plan:
                say(f"  {C['y']}Didn't understand that — try being more specific.{C['0']}")
                continue
            built = topic_from_plan(plan, desc)
            say(f"  {C['g']}✓ {built[0]['title']}{C['0']}  {C['d']}{describe(built)}{C['0']}")
            if ask_yn("    Keep it?", True):
                topics.extend(built)
    if not topics:
        while True:
            topics.extend(manual_topic(len(topics) + 1))
            if not ask_yn(f"\n  Add another topic? {C['d']}({len(topics)} so far){C['0']}",
                          False):
                break

    rule("Delivery")
    method = ask_choice("  Where should the digest go?", [
        ("file", "A file", "data/preview.html — no extra key, easiest to start"),
        ("resend", "Email", "needs a free Resend key"),
        ("webhook", "Webhook", "Slack, Discord, or your own endpoint"),
    ])
    delivery = {"method": "file"}
    if method == "resend":
        to = ask("    Your email address")
        if to:
            delivery = {"method": "resend", "to": to,
                        "from": "onboarding@resend.dev"}
        else:
            say(f"    {C['d']}No address — using a file instead. "
                f"Add it to digest.yaml later.{C['0']}")
    elif method == "webhook":
        url = ask("    Webhook URL")
        if url:
            delivery = {"method": "webhook", "url": url}
        else:
            say(f"    {C['d']}No URL — using a file instead.{C['0']}")

    if key and ask_yn(f"\n  Use a different model? "
                      f"{C['d']}(default: DeepSeek V4 Pro, ~$4/year){C['0']}", False):
        model = ask_choice("  Which model?", MODEL_MENU)

    cfg = {"title": title, "language": language, "headline": True,
           "output_dir": "data",
           "llm": {"model": model, "fallback_model": "anthropic/claude-fable-5",
                   "max_tokens": 2000},
           "delivery": delivery, "topics": topics}
    body = yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False, width=100)
    out.write_text(body, encoding="utf-8")

    rule("Done")
    say(f"{C['g']}Wrote {out}{C['0']} — {len(topics)} sections\n")
    for t in topics:
        say(f"  {C['b']}{t['title']}{C['0']}  {C['d']}{describe([t])}{C['0']}")

    needs = []
    if not os.environ.get("OPENROUTER_API_KEY"):
        needs.append(("OPENROUTER_API_KEY", "https://openrouter.ai/keys"))
    if delivery["method"] == "resend" and not os.environ.get("RESEND_API_KEY"):
        needs.append(("RESEND_API_KEY", "https://resend.com/api-keys"))
    if needs:
        env = pathlib.Path(".env")
        body_env = env.read_text(encoding="utf-8") if env.exists() else ""
        for k, _ in needs:
            if f"{k}=" not in body_env:
                body_env += f"{k}=\n"
        env.write_text(body_env, encoding="utf-8")
        say(f"\n{C['y']}Still needed in .env:{C['0']}")
        for k, link in needs:
            say(f"  {k}  {C['c']}{link}{C['0']}")

    say(f"\nNext: {C['b']}python main.py --print{C['0']}  "
        f"{C['d']}(builds it and prints the result, sends nothing){C['0']}")
    say(f"{C['d']}Not quite right? {out} is plain YAML — edit `focus` and "
        f"`queries` directly. See SOURCES.md.{C['0']}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Interactive setup for open-digest")
    p.add_argument("-o", "--out", default="digest.yaml")
    p.add_argument("--manual", action="store_true",
                   help="skip the AI planning step and answer everything yourself")
    a = p.parse_args()
    try:
        main(a.out, a.manual)
    except KeyboardInterrupt:
        say("\nCancelled.")
