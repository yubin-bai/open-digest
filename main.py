"""open-digest — build a daily digest from topics you define.

    python wizard.py               # interactive setup, writes digest.yaml
    python main.py --dry-run       # fetch + summarize, save preview, don't deliver
    python main.py --print         # dry run, also print the digest to the terminal
    python main.py                 # full run, deliver
    python main.py -c work.yaml    # use a different config

The pipeline: config -> sources -> LLM per topic -> JSON -> HTML -> delivery.
Nothing about the *content* lives in the code; it all comes from the config.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys

import yaml
from dotenv import load_dotenv

import render
import sources
import summarizer

load_dotenv()  # local .env; real environment variables win


def load_config(path: str) -> dict:
    p = pathlib.Path(path)
    if not p.exists():
        sys.exit(f"Config not found: {p}\nRun `python wizard.py` to create one.")
    cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not cfg.get("topics"):
        sys.exit(f"{p} has no `topics:`. Run `python wizard.py` to build one.")
    return cfg


def collect(topic: dict) -> list:
    """Fetch every source of a topic, de-duplicated by URL."""
    items, seen = [], set()
    for spec in topic.get("sources", []):
        for it in sources.fetch(spec):
            url = it.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            items.append(it)
    return items


def build(cfg: dict) -> dict:
    """Run the whole pipeline and return the digest object (also the JSON on disk)."""
    llm_cfg = cfg.get("llm", {})
    model = llm_cfg.get("model", "deepseek/deepseek-v4-pro")
    fallback = llm_cfg.get("fallback_model")
    language = cfg.get("language", "English")
    topics = cfg["topics"]

    print(f"== 1/3 Fetching sources ({len(topics)} topics) ==")
    raw = {}
    for t in topics:
        tid = t.get("id") or t.get("title", "topic")
        t["id"] = tid
        if t.get("kind") in ("tickers", "countdown"):
            continue  # widgets are fetched in step 3, they skip the LLM
        raw[tid] = collect(t)
        print(f"   {tid}: {len(raw[tid])} items")

    print(f"== 2/3 Summarizing (model={model}) ==")
    summaries = {}
    for t in topics:
        tid = t["id"]
        if tid not in raw:
            continue
        items = summarizer.summarize_topic(
            t, raw[tid], model, language, llm_cfg.get("max_tokens", 2000))
        if not items and fallback and fallback != model and raw[tid]:
            print(f"   [warn] falling back to {fallback} for '{tid}'")
            items = summarizer.summarize_topic(
                t, raw[tid], fallback, language, llm_cfg.get("max_tokens", 2000))
        summaries[tid] = items
        print(f"   {tid}: {len(items)} selected")

    print("== 3/3 Assembling ==")
    sections = []
    for t in topics:
        tid, kind = t["id"], t.get("kind", "items")
        title = t.get("title", tid)
        if kind == "tickers":
            rows = sources.tickers(t.get("symbols", []), t.get("news_per_ticker", 2))
            if rows:
                sections.append({"id": tid, "title": title, "kind": kind, "rows": rows})
        elif kind == "countdown":
            rows = sources.countdown(t.get("deadlines", []))
            if rows:
                sections.append({"id": tid, "title": title, "kind": kind, "rows": rows})
        elif summaries.get(tid):
            sections.append({"id": tid, "title": title, "kind": "items",
                             "items": summaries[tid]})

    headline = ""
    if cfg.get("headline", True):
        headline = summarizer.make_headline(summaries, model, language)

    return {"date": dt.date.today().isoformat(),
            "title": cfg.get("title", "Daily Digest"),
            "headline": headline,
            "sections": sections}


def main(config="digest.yaml", dry_run=False, show=False):
    cfg = load_config(config)
    digest = build(cfg)

    if not digest["sections"]:
        print("\nNo sections had content today. Nothing to deliver.")
        print("Check the [warn] lines above — usually a source returned nothing "
              "or the LLM key is missing.")
        return

    out_dir = pathlib.Path(cfg.get("output_dir", "data"))
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{digest['date']}.json"
    json_path.write_text(json.dumps(digest, ensure_ascii=False, indent=2),
                         encoding="utf-8")

    html_body = render.render_html(digest, digest["title"])
    (out_dir / "preview.html").write_text(html_body, encoding="utf-8")
    print(f"   saved -> {json_path} and {out_dir / 'preview.html'}")

    if show:
        print("\n" + render.render_text(digest, digest["title"]))

    if dry_run or show:
        print("\n[dry-run] Nothing delivered. Open the preview file to review.")
        return

    delivery = dict(cfg.get("delivery", {"method": "file"}))
    delivery["_text"] = render.render_text(digest, digest["title"])
    print(f"== Delivering via {delivery.get('method', 'file')} ==")
    render.deliver(html_body, f"{digest['title']} · {digest['date']}", delivery)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-c", "--config", default="digest.yaml")
    p.add_argument("--dry-run", action="store_true",
                   help="build and save, but do not deliver")
    p.add_argument("--print", dest="show", action="store_true",
                   help="dry run and print the digest to stdout")
    a = p.parse_args()
    main(config=a.config, dry_run=a.dry_run, show=a.show)
