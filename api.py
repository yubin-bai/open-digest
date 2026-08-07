"""HTTP API for the mini program.

    pip install flask
    python api.py                      # local, http://127.0.0.1:8080

Endpoints — all JSON, all authenticated by openid:

    GET  /health                       liveness probe for the platform
    POST /api/session                  {code?} -> {new_user, topics, max_topics}
    GET  /api/catalog                  the preset picker, grouped
    POST /api/topics                   {topics: [...]} -> saved selection
    POST /api/topics/custom            {description} -> a custom topic key
    GET  /api/digest                   today's digest for this user
    GET  /api/digest?format=html       the same, rendered

Identity comes from `X-WX-OPENID`, which WeChat Cloud Run injects on every
`callContainer` request. Outside Cloud Run the client posts its `wx.login()`
code to `/api/session` and gets a signed token back for subsequent calls. See
wechat.py.

Reading is cheap here on purpose: `/api/digest` never calls an LLM. It reads
rows the nightly batch already wrote, so a traffic spike costs database reads,
not API spend. The only endpoint that can trigger model use is
`/api/topics/custom`, which is rate-limited per user.
"""

from __future__ import annotations

import functools
import os
import time

from flask import Flask, g, jsonify, request

import render
import service
import wechat
from store import MAX_TOPICS, Catalog, Store

DB_PATH = os.environ.get("DIGEST_DB", "digest.db")
CATALOG_PATH = os.environ.get("DIGEST_CATALOG", "catalog.yaml")
TITLE = os.environ.get("DIGEST_TITLE", "每日 Digest")
DEFAULT_LANGUAGE = os.environ.get("DIGEST_LANGUAGE",
                                  "Chinese, keep technical terms in English")
CUSTOM_TOPIC_COOLDOWN = int(os.environ.get("CUSTOM_TOPIC_COOLDOWN", "60"))

# Hard ceiling on how many custom topics the whole platform will build in a day.
# Custom topics are the only endpoint that spends money, and per-user throttling
# doesn't help against someone rotating openids — which is possible whenever the
# public test URL is enabled, since X-WX-OPENID is only trustworthy when the
# platform injects it on a callContainer request. This bounds the damage to a
# known number of calls no matter what.
CUSTOM_TOPIC_DAILY_CAP = int(os.environ.get("CUSTOM_TOPIC_DAILY_CAP", "200"))

app = Flask(__name__)
store = Store(DB_PATH)
catalog = Catalog(CATALOG_PATH, store)

_last_custom: dict[str, float] = {}
_custom_budget = {"day": None, "used": 0}


def _spend_custom_budget() -> bool:
    """Consume one unit of today's global custom-topic budget."""
    from store import today as _today
    day = _today()
    if _custom_budget["day"] != day:
        _custom_budget.update(day=day, used=0)
    if _custom_budget["used"] >= CUSTOM_TOPIC_DAILY_CAP:
        return False
    _custom_budget["used"] += 1
    return True


def error(msg, code=400):
    return jsonify({"ok": False, "error": msg}), code


def ok(**payload):
    return jsonify({"ok": True, **payload})


def require_openid(fn):
    """Resolve the caller, or 401. Every /api route except catalog needs this."""
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        body = request.get_json(silent=True) or {}
        try:
            openid = wechat.openid_from_request(request.headers, body.get("code"))
        except RuntimeError as e:
            return error(str(e), 500)
        except Exception as e:  # noqa: BLE001 - upstream network failure
            return error(f"login failed: {e}", 502)
        if not openid:
            return error("not signed in: no X-WX-OPENID header and no login code",
                         401)
        g.openid = openid
        return fn(*a, **kw)
    return wrapper


# --------------------------------------------------------------------------
@app.get("/")
def index():
    """Landing page. Exists so hitting the bare URL tells you something useful
    instead of a bare 404 — the first thing anyone does after deploying."""
    return ok(service="open-digest",
              topics_in_catalog=len(catalog.presets),
              wx_cloud=wechat.in_cloud(),
              endpoints=sorted(
                  str(r.rule) for r in app.url_map.iter_rules()
                  if str(r.rule).startswith(("/api", "/health"))))


@app.get("/health")
def health():
    return ok(service="open-digest", topics_in_catalog=len(catalog.presets))


@app.post("/api/session")
@require_openid
def session():
    """Called on every app open. Tells the client which screen to show."""
    s = service.open_session(store, g.openid)
    return ok(**s)


@app.get("/api/catalog")
def get_catalog():
    """Preset topics, grouped. Public — no identity needed to browse."""
    return ok(groups=catalog.list_for_picker(), max_topics=MAX_TOPICS)


@app.post("/api/topics")
@require_openid
def set_topics():
    """Save a selection. Used for both onboarding and later edits."""
    topics = (request.get_json(silent=True) or {}).get("topics")
    if not isinstance(topics, list):
        return error("body must be {\"topics\": [...]}")
    try:
        saved = service.choose_topics(store, catalog, g.openid, topics)
    except ValueError as e:
        return error(str(e))
    return ok(topics=saved,
              labels=[catalog.label(k) for k in saved])


@app.post("/api/topics/custom")
@require_openid
def custom_topic():
    """Free text -> a registered custom topic key.

    Does NOT subscribe the user; the client shows the preview and then posts to
    /api/topics. Keeping creation and subscription separate means an abandoned
    preview leaves no dangling subscription.
    """
    body = request.get_json(silent=True) or {}
    desc = (body.get("description") or "").strip()
    if not (2 <= len(desc) <= 200):
        return error("description must be 2-200 characters")

    now = time.time()
    if now - _last_custom.get(g.openid, 0) < CUSTOM_TOPIC_COOLDOWN:
        wait = int(CUSTOM_TOPIC_COOLDOWN - (now - _last_custom[g.openid]))
        return error(f"too fast, try again in {wait}s", 429)

    if not _spend_custom_budget():
        return error("今天的自定义方向已达上限，明天再试", 429)

    # UGC that will be displayed — must be moderated. Fails closed.
    allowed, reason = wechat.check_text(desc, g.openid)
    if not allowed:
        return error(f"content rejected ({reason})", 422)

    _last_custom[g.openid] = now
    try:
        topic = service.create_custom_topic(
            store, desc, body.get("language") or DEFAULT_LANGUAGE,
            openid=g.openid)
    except ValueError as e:
        return error(str(e))
    except Exception as e:  # noqa: BLE001 - model or network failure
        return error(f"could not build that topic: {e}", 502)
    return ok(topic=topic)


@app.get("/api/digest")
@require_openid
def digest():
    """Today's digest. Pure read — never calls a model."""
    day = request.args.get("day")
    d = service.assemble(store, catalog, g.openid, TITLE, day)
    if d is None:
        return ok(digest=None,
                  reason="nothing built yet for your topics today")
    if request.args.get("format") == "html":
        return app.response_class(render.render_html(d, TITLE),
                                  mimetype="text/html")
    return ok(digest=d)


@app.post("/api/unsubscribe")
@require_openid
def unsubscribe():
    """Stop receiving without losing the selection."""
    active = bool((request.get_json(silent=True) or {}).get("active", False))
    store.set_active(g.openid, active)
    return ok(active=active)


@app.errorhandler(404)
def not_found(_e):
    return error("no such endpoint", 404)


@app.errorhandler(500)
def server_error(e):
    app.logger.exception("unhandled error")
    return error(f"internal error: {e}", 500)


if __name__ == "__main__":
    # Cloud Run sets PORT. Bind all interfaces so the platform can reach it.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
