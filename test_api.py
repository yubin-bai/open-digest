"""API tests — no network, no WeChat credentials, no LLM calls.

    pip install flask
    python test_api.py

Moderation and the topic planner are stubbed, since both need real credentials.
Everything else is the real Flask app against a real (temporary) database.
"""

import json
import os
import tempfile

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  -> ' + detail}")


# A temp DB must exist before api.py opens one at import time.
_fd, DB = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.unlink(DB)
os.environ["DIGEST_DB"] = DB
os.environ["CUSTOM_TOPIC_COOLDOWN"] = "0"

import api  # noqa: E402
import wechat  # noqa: E402

api.app.config["TESTING"] = True
client = api.app.test_client()

HDR = {"X-WX-OPENID": "wx_alice"}


def call(method, path, body=None, headers=HDR):
    fn = getattr(client, method)
    r = fn(path, json=body, headers=headers) if body is not None \
        else fn(path, headers=headers)
    try:
        return r.status_code, json.loads(r.data)
    except Exception:  # noqa: BLE001 - html responses
        return r.status_code, r.data.decode()


# ---------------------------------------------------------------- auth
def test_auth():
    print("\nauthentication")
    code, body = call("post", "/api/session", headers={})
    check("missing identity is 401", code == 401, f"{code} {body}")
    check("401 explains what's missing", "X-WX-OPENID" in body.get("error", ""))

    code, _ = call("get", "/api/catalog", headers={})
    check("catalog is public", code == 200, str(code))

    code, body = call("post", "/api/session")
    check("X-WX-OPENID header authenticates", code == 200, f"{code} {body}")

    code, body = call("get", "/health", headers={})
    check("health needs no auth and reports the catalog",
          code == 200 and body["topics_in_catalog"] > 0, str(body))

    code, body = call("get", "/api/nope")
    check("unknown endpoint is a clean 404 JSON", code == 404 and not body["ok"])


# ---------------------------------------------------------------- flows
def test_new_user_flow():
    print("\nnew user -> onboarding -> returning user")
    _, s = call("post", "/api/session", {})
    check("first session flags a new user", s["new_user"] and s["topics"] == [],
          str(s))
    check("client is told the limit", s["max_topics"] == 3)

    _, c = call("get", "/api/catalog")
    check("catalog is grouped with labels",
          len(c["groups"]) >= 3 and all(g["topics"] for g in c["groups"]))

    picks = [g["topics"][0]["key"] for g in c["groups"]][:3]
    code, r = call("post", "/api/topics", {"topics": picks})
    check("selection saves", code == 200 and r["topics"] == picks, str(r))
    check("labels come back for display",
          len(r["labels"]) == len(picks) and all(r["labels"]))

    _, s2 = call("post", "/api/session", {})
    check("second session is not new", not s2["new_user"])
    check("saved topics come back in order", s2["topics"] == picks, str(s2))

    for _ in range(3):
        call("post", "/api/session", {})
    _, s3 = call("post", "/api/session", {})
    check("repeated opens never change the selection", s3["topics"] == picks)


def test_limit_enforced_over_http():
    print("\ntopic limit over HTTP")
    code, r = call("post", "/api/topics",
                   {"topics": ["ai", "macro", "dev", "crypto"]})
    check("4 topics is rejected with 400", code == 400, f"{code} {r}")
    check("error names the limit", "3" in r["error"], r.get("error"))

    _, s = call("post", "/api/session", {})
    check("rejection left the old selection intact", len(s["topics"]) == 3)

    code, _ = call("post", "/api/topics", {"topics": []})
    check("empty selection is rejected", code == 400)

    code, _ = call("post", "/api/topics", {"topics": ["ai", "not_a_topic"]})
    check("unknown key is rejected", code == 400)

    code, _ = call("post", "/api/topics", {"topics": "ai"})
    check("wrong body type is rejected", code == 400)

    code, r = call("post", "/api/topics", {"topics": ["ai", "ai", "macro"]})
    check("duplicates collapse", code == 200 and r["topics"] == ["ai", "macro"],
          str(r))


def test_digest_read():
    print("\ndigest read path")
    call("post", "/api/topics", {"topics": ["ai", "macro"]})

    _, r = call("get", "/api/digest")
    check("nothing built yet -> null, not an error",
          r["ok"] and r["digest"] is None, str(r))
    check("reason is human-readable", "nothing built" in r.get("reason", ""))

    api.store.put_render("ai", [{"title": "条目", "summary": "摘要",
                                 "url": "https://a.com/1"}])
    _, r = call("get", "/api/digest")
    d = r["digest"]
    check("built topic appears", d and len(d["sections"]) == 1, str(r))
    check("section carries the human label", d["sections"][0]["title"] == "AI 动态")

    code, html = call("get", "/api/digest?format=html")
    check("html format renders", code == 200 and "AI 动态" in html)

    _, r = call("get", "/api/digest?day=2020-01-01")
    check("an old day is empty, not an error", r["ok"] and r["digest"] is None)


def test_digest_costs_nothing():
    print("\nread path never calls a model")
    import llm
    original = llm.chat

    def explode(*a, **kw):
        raise AssertionError("the read path must not call an LLM")

    llm.chat = explode
    try:
        for _ in range(5):
            code, _ = call("get", "/api/digest")
            if code != 200:
                check("digest served without an LLM call", False, str(code))
                break
        else:
            check("digest served 5x without an LLM call", True)
        code, _ = call("post", "/api/session", {})
        check("session needs no LLM call", code == 200)
        code, _ = call("get", "/api/catalog")
        check("catalog needs no LLM call", code == 200)
    finally:
        llm.chat = original


def test_custom_topic():
    print("\ncustom topic (moderation + planner stubbed)")
    real_check, real_create = wechat.check_text, api.service.create_custom_topic

    wechat.check_text = lambda *a, **kw: (False, "unavailable")
    code, r = call("post", "/api/topics/custom", {"description": "苹果股价走势"})
    check("moderation failure blocks the request", code == 422, f"{code} {r}")
    check("fails closed, not open", "rejected" in r["error"])

    wechat.check_text = lambda *a, **kw: (False, "risky")
    code, _ = call("post", "/api/topics/custom", {"description": "违规内容"})
    check("flagged content is rejected", code == 422)

    wechat.check_text = lambda *a, **kw: (True, "pass")
    for bad, why in [("", "empty"), ("a", "1 char"), ("x" * 500, "too long")]:
        code, _ = call("post", "/api/topics/custom", {"description": bad})
        check(f"{why} description is rejected", code == 400)

    api.service.create_custom_topic = lambda *a, **kw: {
        "key": "custom:deadbeef", "label": "股票", "focus": "Price moves",
        "preview": "news: 苹果 财报"}
    code, r = call("post", "/api/topics/custom", {"description": "苹果股价走势"})
    check("valid description returns a topic", code == 200, f"{code} {r}")
    check("preview is included for confirmation", "preview" in r["topic"])

    _, s = call("post", "/api/session", {})
    check("creating a custom topic does not auto-subscribe",
          "custom:deadbeef" not in s["topics"], str(s["topics"]))

    api.service.create_custom_topic = lambda *a, **kw: (_ for _ in ()).throw(
        ValueError("could not turn that description into a topic"))
    code, _ = call("post", "/api/topics/custom", {"description": "aaaaaa"})
    check("planner failure is a clean 400", code == 400)

    wechat.check_text, api.service.create_custom_topic = real_check, real_create


def test_custom_topic_rate_limit():
    print("\ncustom topic rate limit")
    real_check, real_create = wechat.check_text, api.service.create_custom_topic
    wechat.check_text = lambda *a, **kw: (True, "pass")
    api.service.create_custom_topic = lambda *a, **kw: {
        "key": "custom:aaa", "label": "x", "focus": "y", "preview": "z"}
    api.CUSTOM_TOPIC_COOLDOWN = 60
    api._last_custom.clear()

    code, _ = call("post", "/api/topics/custom", {"description": "第一次请求"})
    check("first request goes through", code == 200, str(code))
    code, r = call("post", "/api/topics/custom", {"description": "第二次请求"})
    check("immediate retry is throttled", code == 429, f"{code} {r}")
    check("throttle says how long to wait", "try again in" in r["error"])

    code, _ = call("post", "/api/topics/custom", {"description": "另一个人"},
                   headers={"X-WX-OPENID": "wx_bob"})
    check("throttling is per user, not global", code == 200, str(code))

    api.CUSTOM_TOPIC_COOLDOWN = 0
    wechat.check_text, api.service.create_custom_topic = real_check, real_create


def test_global_spend_cap():
    """Per-user throttling can't stop rotating openids. The global cap can."""
    print("\nglobal daily spend cap")
    real_check, real_create = wechat.check_text, api.service.create_custom_topic
    wechat.check_text = lambda *a, **kw: (True, "pass")
    calls = {"n": 0}

    def counting(*a, **kw):
        calls["n"] += 1
        return {"key": "custom:x", "label": "x", "focus": "y", "preview": "z"}

    api.service.create_custom_topic = counting
    api.CUSTOM_TOPIC_COOLDOWN = 0
    api.CUSTOM_TOPIC_DAILY_CAP = 3
    api._custom_budget.update(day=None, used=0)
    api._last_custom.clear()

    codes = [call("post", "/api/topics/custom", {"description": f"请求 {i}"},
                  headers={"X-WX-OPENID": f"attacker_{i}"})[0]
             for i in range(6)]
    check("first 3 succeed", codes[:3] == [200, 200, 200], str(codes))
    check("the rest are capped", codes[3:] == [429, 429, 429], str(codes))
    check("rotating openids does not spend past the cap",
          calls["n"] == 3, f"{calls['n']} model calls")

    api._custom_budget.update(day=None, used=0)  # simulate the day rolling over
    code, _ = call("post", "/api/topics/custom", {"description": "第二天"},
                   headers={"X-WX-OPENID": "attacker_9"})
    check("budget resets the next day", code == 200, str(code))

    api.CUSTOM_TOPIC_DAILY_CAP = 200
    wechat.check_text, api.service.create_custom_topic = real_check, real_create


def test_unsubscribe():
    print("\nunsubscribe")
    call("post", "/api/topics", {"topics": ["ai", "macro"]})
    code, r = call("post", "/api/unsubscribe", {"active": False})
    check("deactivates", code == 200 and r["active"] is False)
    check("drops out of the batch queue",
          "wx_alice" not in [u["openid"] for u in api.store.active_users()])

    _, s = call("post", "/api/session", {})
    check("topics survive deactivation", s["topics"] == ["ai", "macro"], str(s))

    call("post", "/api/unsubscribe", {"active": True})
    check("reactivates into the queue",
          "wx_alice" in [u["openid"] for u in api.store.active_users()])


def test_isolation():
    print("\nuser isolation")
    call("post", "/api/topics", {"topics": ["ai"]})
    call("post", "/api/topics", {"topics": ["crypto", "dev"]},
         headers={"X-WX-OPENID": "wx_carol"})

    _, a = call("post", "/api/session", {})
    _, c = call("post", "/api/session", {}, headers={"X-WX-OPENID": "wx_carol"})
    check("each user sees only their own topics",
          a["topics"] == ["ai"] and c["topics"] == ["crypto", "dev"],
          f"{a['topics']} / {c['topics']}")

    api.store.put_render("ai", [{"title": "A", "summary": "S", "url": "https://a/1"}])
    _, da = call("get", "/api/digest")
    _, dc = call("get", "/api/digest", headers={"X-WX-OPENID": "wx_carol"})
    check("digests don't leak across users",
          da["digest"] is not None and dc["digest"] is None)


if __name__ == "__main__":
    print("open-digest API tests")
    try:
        test_auth()
        test_new_user_flow()
        test_limit_enforced_over_http()
        test_digest_read()
        test_digest_costs_nothing()
        test_custom_topic()
        test_custom_topic_rate_limit()
        test_global_spend_cap()
        test_unsubscribe()
        test_isolation()
    finally:
        api.store.close()
        if os.path.exists(DB):
            os.unlink(DB)
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("failed: " + ", ".join(FAIL))
    raise SystemExit(1 if FAIL else 0)
