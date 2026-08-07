"""Offline tests — no network, no API key, no cost.

    python test_offline.py

Covers the parts that break silently in production: field mapping, LLM output
validation, URL hallucination, and rendering. Anything that needs the network is
tested against fixtures, not live endpoints.
"""

import json
import sys

import render
import sources
import summarizer

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  -> ' + detail}")


# ---------------------------------------------------------------- json_feed
def test_field_mapping():
    print("\njson_feed field mapping")
    row = {"company_name": "Acme", "title": "SWE Intern",
           "locations": ["Remote", "NYC"], "url": "https://x.com/1"}
    check("template renders",
          sources._render("{company_name} — {title}", row) == "Acme — SWE Intern",
          sources._render("{company_name} — {title}", row))
    check("lists join with commas",
          sources._render("{locations}", row) == "Remote, NYC")
    check("bare key copies field",
          sources._render("url", row) == "https://x.com/1")
    check("missing key is empty, no crash",
          sources._render("{nope}", row) == "")
    check("dangling separator trimmed",
          sources._render("{company_name} — {nope}", row) == "Acme")


def test_epoch():
    print("\ndate parsing")
    check("epoch seconds", sources._epoch(1750000000) == 1750000000.0)
    check("ISO with Z", sources._epoch("2026-01-01T00:00:00Z") is not None)
    check("garbage -> None", sources._epoch("not a date") is None)
    check("None -> None", sources._epoch(None) is None)
    check("small int rejected as ms/garbage", sources._epoch(42) is None)


def test_source_contract():
    print("\nsource contract")
    check("unknown type returns [] not raise", sources.fetch({"type": "nope"}) == [])
    check("bad kwargs return []",
          sources.fetch({"type": "news", "bogus_arg": 1}) == [])
    check("every registered type is callable",
          all(callable(f) for f in sources.REGISTRY.values()))
    check("empty input short-circuits", sources.news([]) == [])


# ---------------------------------------------------------------- validation
VALID_URLS = {"https://a.com/1", "https://a.com/2"}


def test_parse_validate():
    print("\nLLM output validation")

    ok = json.dumps({"items": [
        {"title": "T1", "summary": "S1", "url": "https://a.com/1"}]})
    check("clean JSON parses",
          len(summarizer._parse_validate(ok, VALID_URLS)) == 1)

    fenced = "```json\n" + ok + "\n```"
    check("code fence stripped",
          len(summarizer._parse_validate(fenced, VALID_URLS)) == 1)

    chatty = "Sure, here you go:\n" + ok + "\nHope that helps!"
    check("surrounding prose ignored",
          len(summarizer._parse_validate(chatty, VALID_URLS)) == 1)

    trailing = '{"items": [{"title": "T", "summary": "S", "url": "https://a.com/1",}]}'
    check("trailing comma repaired",
          len(summarizer._parse_validate(trailing, VALID_URLS)) == 1)

    double = json.dumps({"items": json.dumps(
        [{"title": "T", "summary": "S", "url": "https://a.com/1"}])})
    check("double-encoded array recovered",
          len(summarizer._parse_validate(double, VALID_URLS)) == 1)

    mixed = json.dumps({"items": [
        {"title": "good", "summary": "S", "url": "https://a.com/1"},
        {"title": "no url", "summary": "S"},
        {"title": "no summary", "url": "https://a.com/2"},
        "a bare string",
    ]})
    got = summarizer._parse_validate(mixed, VALID_URLS)
    check("incomplete items dropped, good one kept",
          len(got) == 1 and got[0]["title"] == "good", str(got))

    halluc = json.dumps({"items": [
        {"title": "real", "summary": "S", "url": "https://a.com/1"},
        {"title": "invented", "summary": "S", "url": "https://fake.com/999"}]})
    got = summarizer._parse_validate(halluc, VALID_URLS)
    check("URL not in material is rejected",
          len(got) == 1 and got[0]["title"] == "real", str(got))

    check("note field accepted as summary alias",
          len(summarizer._parse_validate(
              json.dumps({"items": [{"title": "T", "note": "N",
                                     "url": "https://a.com/1"}]}), VALID_URLS)) == 1)

    for name, bad in [("not JSON", "I could not complete that request."),
                      ("empty items", '{"items": []}'),
                      ("items is a number", '{"items": 5}'),
                      ("all items unusable", '{"items": [{"title": "x"}]}')]:
        try:
            summarizer._parse_validate(bad, VALID_URLS)
            check(f"{name} raises ValueError", False, "no exception")
        except ValueError:
            check(f"{name} raises ValueError", True)


def test_empty_topic_skips_llm():
    print("\npipeline guards")
    check("no items -> no LLM call",
          summarizer.summarize_topic({"id": "t"}, [], "fake/model") == [])
    check("no titles -> empty headline",
          summarizer.make_headline({}, "fake/model") == "")


# ---------------------------------------------------------------- rendering
DIGEST = {
    "date": "2026-01-15", "title": "Test Digest", "headline": "Something happened.",
    "sections": [
        {"id": "a", "title": "Topic A", "kind": "items", "items": [
            {"title": "Item <one>", "summary": "A & B", "url": "https://a.com/1"}]},
        {"id": "m", "title": "Markets", "kind": "tickers", "rows": [
            {"ticker": "NVDA", "close": 100.0, "change_pct": -1.5, "currency": "USD",
             "news": [{"title": "News", "url": "https://n.com"}]}]},
        {"id": "d", "title": "Deadlines", "kind": "countdown", "rows": [
            {"name": "Thing", "date": "2026-02-01", "days_left": 17}]},
    ],
}


def test_render():
    print("\nrendering")
    h = render.render_html(DIGEST, "Test Digest")
    check("headline present", "Something happened." in h)
    check("item section rendered", "Topic A" in h and "https://a.com/1" in h)
    check("ticker section rendered", "NVDA" in h and "-1.50%" in h)
    check("countdown rendered", "17 days left" in h)
    check("HTML in content is escaped",
          "&lt;one&gt;" in h and "<one>" not in h)
    check("ampersand escaped", "A &amp; B" in h)

    t = render.render_text(DIGEST, "Test Digest")
    check("text has all sections",
          all(s in t for s in ("Topic A", "Markets", "Deadlines")))

    empty = render.render_html({"date": "2026-01-15", "sections": []}, "Empty")
    check("empty digest still valid HTML", empty.strip().startswith("<!doctype html>"))

    check("unknown delivery method raises",
          _raises(lambda: render.deliver("x", "y", {"method": "carrier-pigeon"})))
    check("file method is a no-op",
          render.deliver("x", "y", {"method": "file"}) is None)


def _raises(fn):
    try:
        fn()
        return False
    except Exception:  # noqa: BLE001
        return True


# ---------------------------------------------------------------- configs
def test_example_configs():
    print("\nexample configs")
    import pathlib

    import yaml
    files = ([pathlib.Path("digest.example.yaml")]
             + sorted(pathlib.Path("examples").glob("*.yaml")))
    for f in files:
        if not f.exists():
            check(f"{f} exists", False, "missing")
            continue
        try:
            cfg = yaml.safe_load(f.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            check(f"{f} parses", False, str(e))
            continue
        topics = cfg.get("topics", [])
        bad_types = [s.get("type") for t in topics for s in t.get("sources", [])
                     if s.get("type") not in sources.REGISTRY]
        check(f"{f.name}: parses, {len(topics)} topics, all source types known",
              bool(topics) and not bad_types, f"unknown: {bad_types}")


# ---------------------------------------------------------------- wizard
def test_wizard_input_normalization():
    print("\nwizard input handling")
    import re

    import wizard
    for raw in ["1,3", "1，3", "1 3", "1、3", "1;3", "1，  3"]:
        picks = [p for p in re.split(r"[,\s]+", raw.translate(wizard.SEPARATORS)) if p]
        check(f"{raw!r} parses as two picks", picks == ["1", "3"], str(picks))
    check("slug strips punctuation", wizard.slug("AI & ML!") == "ai_ml")
    check("slug keeps CJK", wizard.slug("股票") == "股票")
    check("slug never returns empty", wizard.slug("!!!") == "topic")


def test_wizard_plan_to_topic():
    print("\nwizard plan -> config")
    import wizard
    plan = {"title": "股票", "focus": "Price moves and earnings.",
            "queries": ["苹果 财报", "中国船舶 股价"], "subreddits": ["stocks"],
            "arxiv_categories": [], "hn_keywords": [],
            "tickers": ["AAPL", "600150.SS"], "max_items": 4}

    built = wizard.topic_from_plan(plan, "苹果和中国船舶")
    check("tickers produce a second widget block",
          len(built) == 2 and built[1]["kind"] == "tickers")
    check("empty source lists are omitted",
          {s["type"] for s in built[0]["sources"]} == {"news", "reddit"},
          str([s["type"] for s in built[0]["sources"]]))
    check("no tickers -> one block",
          len(wizard.topic_from_plan(dict(plan, tickers=[]), "x")) == 1)
    check("max_items clamped high",
          wizard.topic_from_plan(dict(plan, max_items=99), "x")[0]["max_items"] == 10)
    check("max_items clamped low",
          wizard.topic_from_plan(dict(plan, max_items=0), "x")[0]["max_items"] == 5)
    check("empty queries fall back to the description",
          wizard.topic_from_plan(dict(plan, queries=[]),
                                 "desc")[0]["sources"][0]["queries"] == ["desc"])
    check("missing keys don't crash",
          len(wizard.topic_from_plan({}, "bare description")) == 1)

    for t in built:
        if t.get("kind"):
            continue
        bad = [s["type"] for s in t["sources"] if s["type"] not in sources.REGISTRY]
        check("planned source types are all real", not bad, str(bad))
    check("describe() summarizes without crashing",
          "苹果 财报" in wizard.describe(built))


if __name__ == "__main__":
    print("open-digest offline tests")
    test_field_mapping()
    test_epoch()
    test_source_contract()
    test_parse_validate()
    test_empty_topic_skips_llm()
    test_render()
    test_example_configs()
    test_wizard_input_normalization()
    test_wizard_plan_to_topic()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("failed: " + ", ".join(FAIL))
    sys.exit(1 if FAIL else 0)
