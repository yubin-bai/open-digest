"""Multi-user layer — onboarding, and the daily batch that serves everyone.

    python service.py build          # build today's topics, assemble every user
    python service.py build --user X # one user, for debugging
    python service.py stats          # subscribers per topic, delivery status
    python service.py demo           # create a fake user end-to-end

The shape of the day:

    topics_in_use()  ->  fetch + summarize each ONCE  ->  store in renders
                                                              |
    for each active user: pull their <=3 renders, assemble their digest

Cost scales with *distinct topics*, not users. Ten thousand people sharing three
presets is three summarization jobs. That property is the whole reason the
catalog exists, and it's why `build_topics` runs before any user is touched.

The single-user CLI (`main.py`) is untouched and still works standalone.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

import main as pipeline
import render
import sources
import summarizer
from store import MAX_TOPICS, Catalog, Store, today

DEFAULT_MODEL = "deepseek/deepseek-v4-pro"
DEFAULT_LANGUAGE = "Chinese, keep technical terms in English"


# --------------------------------------------------------------------------
# Onboarding
# --------------------------------------------------------------------------
def open_session(store: Store, openid: str) -> dict:
    """Called on every app open. Decides new-user vs returning-user.

    Returning users get their saved selection back verbatim — this function has
    no path that modifies it.
    """
    user = store.ensure_user(openid)
    topics = store.get_topics(openid)
    return {"openid": openid, "new_user": not user["onboarded"],
            "topics": topics, "language": user["language"],
            "max_topics": MAX_TOPICS}


def choose_topics(store: Store, catalog: Catalog, openid: str,
                  topic_keys: list) -> list:
    """Save a user's picks. Works for both onboarding and later edits."""
    store.ensure_user(openid)
    unknown = [k for k in topic_keys if catalog.resolve(k) is None]
    if unknown:
        raise ValueError(f"unknown topic(s): {', '.join(unknown)}")
    return store.set_topics(openid, topic_keys)


def create_custom_topic(store: Store, description: str, language: str,
                        model: str = DEFAULT_MODEL, openid: str = None) -> dict:
    """Free-text description -> a registered custom topic.

    Reuses the wizard's planner, so a user typing a sentence in the mini program
    goes through exactly the code path tested on the CLI. Content-addressed: two
    users describing the same thing get one topic and one daily cost.
    """
    import wizard
    plan = wizard.plan_topic(description, language, model)
    if not plan:
        raise ValueError("could not turn that description into a topic")
    built = wizard.topic_from_plan(plan, description)[0]  # ignore the ticker widget
    spec = {"focus": built["focus"], "max_items": built["max_items"],
            "sources": built["sources"]}
    key = store.add_custom_topic(built["title"], spec, created_by=openid)
    return {"key": key, "label": built["title"], "focus": spec["focus"],
            "preview": wizard.describe([built])}


# --------------------------------------------------------------------------
# Daily batch
# --------------------------------------------------------------------------
def build_topics(store: Store, catalog: Catalog, model: str = DEFAULT_MODEL,
                 language: str = DEFAULT_LANGUAGE, day: str = None,
                 force: bool = False) -> dict:
    """Fetch + summarize every topic in use, once. Returns {key: item_count}."""
    day = day or today()
    keys = store.topics_in_use()
    counts = store.subscriber_counts()
    print(f"== Building {len(keys)} distinct topics for {day} ==")

    results = {}
    for key in keys:
        if not force and store.get_render(key, day) is not None:
            results[key] = len(store.get_render(key, day))
            print(f"   {key}: cached ({results[key]} items)")
            continue
        spec = catalog.resolve(key)
        if spec is None:
            # A preset was removed from catalog.yaml while people still had it.
            print(f"   [warn] {key}: no longer in the catalog, skipping")
            results[key] = 0
            continue

        # Widget topics render as tables and skip the LLM entirely. They're
        # cached like everything else so the read path stays a pure lookup.
        kind = spec.get("kind", "items")
        if kind == "tickers":
            rows = sources.tickers(spec.get("symbols", []),
                                   spec.get("news_per_ticker", 2))
        elif kind == "countdown":
            rows = sources.countdown(spec.get("deadlines", []))
        else:
            raw = pipeline.collect(spec)
            rows = summarizer.summarize_topic(spec, raw, model, language)
            print(f"   {key}: {len(raw)} fetched -> {len(rows)} kept "
                  f"({counts.get(key, 0)} subscribers)")
        if kind != "items":
            print(f"   {key}: {len(rows)} rows ({kind}, no LLM call)")
        store.put_render(key, rows, day)
        results[key] = len(rows)
    return results


def assemble(store: Store, catalog: Catalog, openid: str, title: str,
             day: str = None) -> dict | None:
    """Build one user's digest from already-rendered topics. No LLM calls.

    Returns None when the user has nothing to read today — better to send
    nothing than to send an empty email.
    """
    day = day or today()
    sections = []
    for key in store.get_topics(openid):
        rows = store.get_render(key, day)
        if not rows:
            continue
        spec = catalog.resolve(key) or {}
        kind = spec.get("kind", "items")
        section = {"id": key, "title": catalog.label(key), "kind": kind}
        # Widgets carry `rows`, summarized topics carry `items` — render.py and
        # the mini program both branch on `kind`, so the shapes must match.
        section["rows" if kind != "items" else "items"] = rows
        sections.append(section)
    if not sections:
        return None
    return {"date": day, "title": title, "headline": "", "sections": sections}


def run_daily(store: Store, catalog: Catalog, title="每日 Digest",
              model=DEFAULT_MODEL, day=None, dry_run=True, only_user=None,
              force=False) -> dict:
    """The whole day. Idempotent: re-running skips users already delivered."""
    day = day or today()
    build_topics(store, catalog, model=model, day=day, force=force)

    users = ([{"openid": only_user}] if only_user else store.active_users())
    print(f"\n== Assembling for {len(users)} user(s) ==")
    stats = {"built": 0, "empty": 0, "skipped": 0}
    for u in users:
        openid = u["openid"]
        if not dry_run and store.delivered_today(openid, day):
            stats["skipped"] += 1  # retry-safe: never double-send
            continue
        digest = assemble(store, catalog, openid, title, day)
        if digest is None:
            store.mark_delivery(openid, "empty", "no items in any topic", day)
            stats["empty"] += 1
            continue
        html = render.render_html(digest, title)
        store.mark_delivery(openid, "built", f"{len(digest['sections'])} sections", day)
        stats["built"] += 1
        if dry_run:
            out = pathlib.Path("data") / f"user_{openid}_{day}.html"
            out.parent.mkdir(exist_ok=True)
            out.write_text(html, encoding="utf-8")
    print(f"   built={stats['built']} empty={stats['empty']} "
          f"skipped={stats['skipped']}")

    store.purge_renders(keep_days=14)
    return stats


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def cmd_stats(store: Store, catalog: Catalog, _args):
    counts = store.subscriber_counts()
    users = store.active_users()
    print(f"{len(users)} active users, {len(counts)} topics in use\n")
    for key, n in counts.items():
        built = store.get_render(key)
        state = f"{len(built)} items today" if built is not None else "not built"
        print(f"  {n:5}  {catalog.label(key):<20} {key:<26} {state}")
    print(f"\ndeliveries today: {store.delivery_stats() or 'none'}")
    print(f"cost model: {len(counts)} summarization calls/day regardless of "
          f"user count")


def cmd_demo(store: Store, catalog: Catalog, args):
    """End-to-end smoke test with a fake user, no network needed to inspect."""
    openid = args.user or "demo_user"
    s = open_session(store, openid)
    print(f"session: new_user={s['new_user']} topics={s['topics']}")
    if s["new_user"]:
        picks = [g["topics"][0]["key"] for g in catalog.list_for_picker()][:MAX_TOPICS]
        print(f"onboarding -> {picks}")
        choose_topics(store, catalog, openid, picks)
    s2 = open_session(store, openid)
    print(f"reopened:  new_user={s2['new_user']} topics={s2['topics']}  "
          f"<- unchanged for returning users")


def cmd_build(store: Store, catalog: Catalog, args):
    run_daily(store, catalog, title=args.title, model=args.model,
              dry_run=not args.send, only_user=args.user, force=args.force)


def cmd_picker(store: Store, catalog: Catalog, _args):
    print(json.dumps(catalog.list_for_picker(), ensure_ascii=False, indent=2))


def build_parser():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default="digest.db")
    p.add_argument("--catalog", default="catalog.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="run the daily batch")
    b.add_argument("--title", default="每日 Digest")
    b.add_argument("--model", default=DEFAULT_MODEL)
    b.add_argument("--user", help="only this openid")
    b.add_argument("--send", action="store_true", help="actually deliver")
    b.add_argument("--force", action="store_true", help="ignore today's cache")
    b.set_defaults(fn=cmd_build)

    s = sub.add_parser("stats", help="subscribers per topic")
    s.set_defaults(fn=cmd_stats)

    d = sub.add_parser("demo", help="create a fake user end to end")
    d.add_argument("--user")
    d.set_defaults(fn=cmd_demo)

    k = sub.add_parser("picker", help="dump the onboarding list as JSON")
    k.set_defaults(fn=cmd_picker)
    return p


if __name__ == "__main__":
    args = build_parser().parse_args()
    store = Store(args.db)
    try:
        args.fn(store, Catalog(args.catalog, store), args)
    except ValueError as e:
        sys.exit(f"error: {e}")
