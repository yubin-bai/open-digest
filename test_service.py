"""Multi-user layer tests — no network, no API key, no cost.

    python test_service.py

The invariants that matter once real users exist:
  * nobody ends up with more than MAX_TOPICS
  * a returning user's selection is byte-identical to what they saved
  * a topic is summarized once per day no matter how many people want it
  * re-running the batch never double-sends
"""

import os
import tempfile

import service
from store import MAX_TOPICS, Catalog, Store

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  -> ' + detail}")


def fresh():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    store = Store(path)
    return store, Catalog("catalog.yaml", store), path


def raises(fn, want=ValueError):
    try:
        fn()
        return False
    except want:
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- catalog
def test_catalog():
    print("\ncatalog")
    store, cat, _ = fresh()
    groups = cat.list_for_picker()
    check("picker returns groups", len(groups) >= 3, str(len(groups)))
    check("every group has topics", all(g["topics"] for g in groups))
    check("picker entries carry label and blurb",
          all("label" in t and "blurb" in t
              for g in groups for t in g["topics"]))

    keys = [t["key"] for g in groups for t in g["topics"]]
    check("preset keys are unique", len(keys) == len(set(keys)))
    for k in keys:
        spec = cat.resolve(k)
        if not (spec and spec.get("focus") and spec.get("sources")):
            check(f"preset {k} resolves to a runnable spec", False, str(spec))
            break
    else:
        check("every preset resolves to a runnable spec", True)

    import sources
    bad = [s.get("type") for k in keys for s in (cat.resolve(k)["sources"])
           if s.get("type") not in sources.REGISTRY]
    check("every preset source type is registered", not bad, str(bad))
    check("unknown key resolves to None", cat.resolve("nope") is None)


# ---------------------------------------------------------------- users
def test_new_vs_returning():
    print("\nnew user vs returning user")
    store, cat, _ = fresh()

    s1 = service.open_session(store, "u1")
    check("first open flags a new user", s1["new_user"] and s1["topics"] == [])
    check("session advertises the limit", s1["max_topics"] == MAX_TOPICS)

    service.choose_topics(store, cat, "u1", ["ai", "macro"])
    s2 = service.open_session(store, "u1")
    check("second open is not a new user", not s2["new_user"])
    check("saved selection comes back in order",
          s2["topics"] == ["ai", "macro"], str(s2["topics"]))

    for _ in range(3):
        service.open_session(store, "u1")
    check("repeated opens never alter the selection",
          store.get_topics("u1") == ["ai", "macro"], str(store.get_topics("u1")))

    store.set_active("u1", False)
    store.set_active("u1", True)
    check("deactivate/reactivate preserves topics",
          store.get_topics("u1") == ["ai", "macro"])

    service.choose_topics(store, cat, "u1", ["dev"])
    check("explicit re-pick replaces the set",
          store.get_topics("u1") == ["dev"], str(store.get_topics("u1")))


def test_topic_limit():
    print("\ntopic limit")
    store, cat, _ = fresh()
    service.open_session(store, "u1")

    check(f"exactly {MAX_TOPICS} is allowed",
          len(service.choose_topics(store, cat, "u1",
                                    ["ai", "macro", "dev"])) == MAX_TOPICS)
    check("more than the limit is rejected",
          raises(lambda: service.choose_topics(
              store, cat, "u1", ["ai", "macro", "dev", "crypto"])))
    check("rejection leaves the old selection intact",
          store.get_topics("u1") == ["ai", "macro", "dev"])
    check("duplicates collapse instead of counting twice",
          service.choose_topics(store, cat, "u1", ["ai", "ai", "macro"])
          == ["ai", "macro"])
    check("empty selection is rejected",
          raises(lambda: service.choose_topics(store, cat, "u1", [])))
    check("unknown topic key is rejected",
          raises(lambda: service.choose_topics(store, cat, "u1", ["ai", "bogus"])))
    check("no append path exists past the limit",
          len(store.get_topics("u1")) <= MAX_TOPICS)


def test_custom_topics():
    print("\ncustom topics")
    store, cat, _ = fresh()
    spec = {"focus": "Apple and shipbuilding stock moves",
            "max_items": 4,
            "sources": [{"type": "news", "queries": ["苹果 财报"]}]}

    k1 = store.add_custom_topic("股票", spec)
    k2 = store.add_custom_topic("股票", dict(spec))
    check("identical specs collapse to one key", k1 == k2, f"{k1} vs {k2}")
    check("key is namespaced", k1.startswith("custom:"))

    k3 = store.add_custom_topic("别的", dict(spec, focus="Different focus entirely"))
    check("different focus yields a different key", k3 != k1)
    check("case and whitespace do not fork the key",
          store.add_custom_topic("x", dict(spec, focus="  APPLE AND SHIPBUILDING "
                                                       "STOCK MOVES  ")) == k1)

    check("resolves through the catalog",
          cat.resolve(k1) is not None and cat.resolve(k1)["title"] == "股票")
    check("a source is required",
          raises(lambda: store.add_custom_topic("bad", {"focus": "x", "sources": []})))

    service.open_session(store, "u1")
    service.choose_topics(store, cat, "u1", ["ai", k1])
    check("custom topic can be subscribed alongside a preset",
          store.get_topics("u1") == ["ai", k1])
    check("subscribing to an unregistered custom key fails",
          raises(lambda: store.set_topics("u1", ["custom:deadbeefdeadbeef"])))

    check("pruning keeps subscribed customs", store.prune_custom_topics() == 1
          and cat.resolve(k1) is not None)


# ---------------------------------------------------------------- batching
def test_shared_work():
    print("\nshared work (the cost property)")
    store, cat, _ = fresh()
    for i in range(50):
        openid = f"u{i}"
        service.open_session(store, openid)
        service.choose_topics(store, cat, openid, ["ai", "macro"])
    service.open_session(store, "solo")
    service.choose_topics(store, cat, "solo", ["ai", "crypto"])

    keys = store.topics_in_use()
    check("51 users x 2 topics collapse to 3 jobs",
          sorted(keys) == ["ai", "crypto", "macro"], str(sorted(keys)))
    counts = store.subscriber_counts()
    check("subscriber counts are right",
          counts["ai"] == 51 and counts["macro"] == 50 and counts["crypto"] == 1,
          str(counts))

    store.set_active("solo", False)
    check("inactive users drop out of the work queue",
          "crypto" not in store.topics_in_use(), str(store.topics_in_use()))
    check("inactive users still drop out of counts",
          "crypto" not in store.subscriber_counts())


def test_render_cache():
    print("\nrender cache")
    store, cat, _ = fresh()
    check("nothing cached initially", store.get_render("ai") is None)
    items = [{"title": "T", "summary": "S", "url": "https://a.com/1"}]
    store.put_render("ai", items)
    check("round-trips", store.get_render("ai") == items)
    store.put_render("ai", items + items)
    check("re-put replaces rather than duplicating", len(store.get_render("ai")) == 2)
    check("other days are separate", store.get_render("ai", "2020-01-01") is None)
    store.put_render("ai", items, "2020-01-01")
    check("old days purge", store.purge_renders(keep_days=14) == 1
          and store.get_render("ai") is not None)


def test_assemble():
    print("\nper-user assembly")
    store, cat, _ = fresh()
    service.open_session(store, "u1")
    service.choose_topics(store, cat, "u1", ["ai", "macro", "dev"])
    store.put_render("ai", [{"title": "A", "summary": "S", "url": "https://a/1"}])
    store.put_render("macro", [{"title": "M", "summary": "S", "url": "https://a/2"}])
    # `dev` deliberately left unbuilt

    d = service.assemble(store, cat, "u1", "Test")
    check("only built topics appear", len(d["sections"]) == 2, str(len(d["sections"])))
    check("sections follow the user's order",
          [s["id"] for s in d["sections"]] == ["ai", "macro"])
    check("section titles are human labels",
          d["sections"][0]["title"] == "AI 动态", d["sections"][0]["title"])

    import render
    html = render.render_html(d, "Test")
    check("assembled digest renders", "AI 动态" in html and "https://a/1" in html)

    service.open_session(store, "u2")
    service.choose_topics(store, cat, "u2", ["crypto"])
    check("user with nothing built gets None",
          service.assemble(store, cat, "u2", "Test") is None)

    store.put_render("crypto", [])  # built, but nothing survived validation
    check("an empty render counts as nothing to read",
          service.assemble(store, cat, "u2", "Test") is None)


def test_widget_sections():
    """Widget topics must reach the client as `rows`, not `items`."""
    print("\nwidget sections")
    store, cat, _ = fresh()
    cat.presets["mkt"] = {"key": "mkt", "label": "行情", "kind": "tickers",
                          "symbols": ["NVDA"], "group": "market"}
    cat.presets["cd"] = {"key": "cd", "label": "倒计时", "kind": "countdown",
                         "deadlines": [{"name": "X", "date": "2030-01-01"}],
                         "group": "life"}
    service.open_session(store, "u1")
    service.choose_topics(store, cat, "u1", ["ai", "mkt", "cd"])

    store.put_render("ai", [{"title": "A", "summary": "S", "url": "https://a/1"}])
    store.put_render("mkt", [{"ticker": "NVDA", "close": 100.0,
                              "change_pct": 1.5, "currency": "USD", "news": []}])
    store.put_render("cd", [{"name": "X", "date": "2030-01-01", "days_left": 900}])

    d = service.assemble(store, cat, "u1", "Test")
    kinds = {s["id"]: s["kind"] for s in d["sections"]}
    check("kinds survive to the client",
          kinds == {"ai": "items", "mkt": "tickers", "cd": "countdown"}, str(kinds))
    by_id = {s["id"]: s for s in d["sections"]}
    check("summarized topic carries `items`", "items" in by_id["ai"])
    check("ticker widget carries `rows`",
          "rows" in by_id["mkt"] and "items" not in by_id["mkt"])
    check("countdown widget carries `rows`", "rows" in by_id["cd"])

    import render as r
    html = r.render_html(d, "Test")
    check("all three kinds render to HTML",
          "NVDA" in html and "900 days left" in html and "https://a/1" in html)


def test_idempotent_delivery():
    print("\ndelivery idempotency")
    store, _, _ = fresh()
    check("not delivered initially", not store.delivered_today("u1"))
    store.mark_delivery("u1", "built", "2 sections")
    check("built is not sent", not store.delivered_today("u1"))
    store.mark_delivery("u1", "sent", "ok")
    check("sent is recorded", store.delivered_today("u1"))
    store.mark_delivery("u1", "sent", "ok")
    check("re-marking stays one row", store.delivery_stats().get("sent") == 1,
          str(store.delivery_stats()))
    check("yesterday is independent", not store.delivered_today("u1", "2020-01-01"))


def test_missing_preset():
    print("\nremoved preset")
    store, cat, _ = fresh()
    service.open_session(store, "u1")
    service.choose_topics(store, cat, "u1", ["ai", "macro"])
    del cat.presets["macro"]  # simulate catalog.yaml losing an entry
    check("resolve returns None for a removed preset", cat.resolve("macro") is None)
    check("label falls back to the key", cat.label("macro") == "macro")
    store.put_render("ai", [{"title": "A", "summary": "S", "url": "https://a/1"}])
    d = service.assemble(store, cat, "u1", "Test")
    check("user still gets their other topics", d is not None
          and len(d["sections"]) == 1)


def test_persistence_across_restart():
    print("\npersistence")
    store, cat, path = fresh()
    service.open_session(store, "u1")
    service.choose_topics(store, cat, "u1", ["ai", "dev"])
    store.close()

    reopened = Store(path)
    check("selection survives a restart",
          reopened.get_topics("u1") == ["ai", "dev"], str(reopened.get_topics("u1")))
    check("user is still onboarded", not reopened.is_new_user("u1"))
    check("unknown user is new", reopened.is_new_user("nobody"))
    reopened.close()
    os.unlink(path)


if __name__ == "__main__":
    print("open-digest multi-user tests")
    test_catalog()
    test_new_vs_returning()
    test_topic_limit()
    test_custom_topics()
    test_shared_work()
    test_render_cache()
    test_assemble()
    test_widget_sections()
    test_idempotent_delivery()
    test_missing_preset()
    test_persistence_across_restart()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("failed: " + ", ".join(FAIL))
    raise SystemExit(1 if FAIL else 0)
