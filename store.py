"""User store — subscriptions, custom topics, and the daily-render cache.

SQLite on purpose: one file, no server, no ops. Every query here is indexed and
none of them are cross-user joins, so this holds tens of thousands of users
before Postgres is worth the trouble. When that day comes, only this module
changes — nothing above it touches SQL.

The two rules that shape the schema:

* **A user has at most MAX_TOPICS subscriptions.** Enforced in `set_topics`,
  which replaces the whole set atomically, so there is no path that appends past
  the limit. Not a DB constraint because "at most 3 rows per user" isn't
  expressible in SQLite without a trigger, and a trigger would fail at INSERT
  time with a message no caller could act on.

* **A returning user's choices never change.** Nothing here mutates
  subscriptions except an explicit `set_topics` call. `ensure_user` on an
  existing openid touches `last_seen` and nothing else.

Topic keys namespace by prefix: presets are bare (``ai``), custom topics are
``custom:<hash>``. The hash is content-derived, so two users who describe the
same thing land on one topic and pay for one summarization.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import sqlite3
import threading

MAX_TOPICS = 3
CUSTOM_PREFIX = "custom:"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    openid      TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    language    TEXT NOT NULL DEFAULT 'Chinese, keep technical terms in English',
    onboarded   INTEGER NOT NULL DEFAULT 0,
    active      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS subscriptions (
    openid      TEXT NOT NULL REFERENCES users(openid) ON DELETE CASCADE,
    topic_key   TEXT NOT NULL,
    position    INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (openid, topic_key)
);
CREATE INDEX IF NOT EXISTS idx_subs_topic ON subscriptions(topic_key);

-- Custom topics are global rows, not per-user: two people who ask for the same
-- thing share one key and therefore one daily summarization.
CREATE TABLE IF NOT EXISTS custom_topics (
    topic_key   TEXT PRIMARY KEY,
    label       TEXT NOT NULL,
    spec        TEXT NOT NULL,          -- JSON: focus, sources, max_items...
    created_by  TEXT,
    created_at  TEXT NOT NULL
);

-- One row per (topic, day). The unit of work the whole platform shares.
CREATE TABLE IF NOT EXISTS renders (
    topic_key   TEXT NOT NULL,
    day         TEXT NOT NULL,
    items       TEXT NOT NULL,          -- JSON array of {title, summary, url}
    built_at    TEXT NOT NULL,
    PRIMARY KEY (topic_key, day)
);

CREATE TABLE IF NOT EXISTS deliveries (
    openid      TEXT NOT NULL,
    day         TEXT NOT NULL,
    status      TEXT NOT NULL,          -- built | sent | failed
    detail      TEXT,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (openid, day)
);
"""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def today() -> str:
    return dt.date.today().isoformat()


class Store:
    """All persistence. Safe to share across threads; each gets its own handle."""

    def __init__(self, path="digest.db"):
        self.path = str(path)
        self._local = threading.local()
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=15)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            try:
                # WAL lets readers run during a write. It needs shared memory,
                # which network and some container mounts don't provide — fall
                # back rather than refusing to start.
                conn.execute("PRAGMA journal_mode = WAL")
            except sqlite3.OperationalError:
                conn.execute("PRAGMA journal_mode = DELETE")
            self._local.conn = conn
        return conn

    def close(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ---------------------------------------------------------------- users
    def ensure_user(self, openid: str, language: str = None) -> dict:
        """Get or create. Returns the user row; `onboarded` tells you which path.

        Deliberately does not touch subscriptions — a returning user's choices
        survive every login.
        """
        if not openid:
            raise ValueError("openid is required")
        with self._conn() as c:
            row = c.execute("SELECT * FROM users WHERE openid = ?", (openid,)).fetchone()
            if row is None:
                fields = {"openid": openid, "created_at": _now(), "last_seen": _now()}
                if language:
                    fields["language"] = language
                cols = ", ".join(fields)
                c.execute(f"INSERT INTO users ({cols}) VALUES "
                          f"({', '.join('?' * len(fields))})", tuple(fields.values()))
                row = c.execute("SELECT * FROM users WHERE openid = ?",
                                (openid,)).fetchone()
            else:
                c.execute("UPDATE users SET last_seen = ? WHERE openid = ?",
                          (_now(), openid))
        return dict(row)

    def is_new_user(self, openid: str) -> bool:
        """True when the user still needs onboarding."""
        with self._conn() as c:
            row = c.execute("SELECT onboarded FROM users WHERE openid = ?",
                            (openid,)).fetchone()
        return row is None or not row["onboarded"]

    def set_language(self, openid: str, language: str):
        with self._conn() as c:
            c.execute("UPDATE users SET language = ? WHERE openid = ?",
                      (language, openid))

    def set_active(self, openid: str, active: bool):
        """Unsubscribe without deleting — keeps their topics for when they return."""
        with self._conn() as c:
            c.execute("UPDATE users SET active = ? WHERE openid = ?",
                      (1 if active else 0, openid))

    def active_users(self) -> list:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM users WHERE active = 1 AND onboarded = 1")]

    # -------------------------------------------------------- subscriptions
    def get_topics(self, openid: str) -> list:
        """Topic keys in the order the user arranged them."""
        with self._conn() as c:
            return [r["topic_key"] for r in c.execute(
                "SELECT topic_key FROM subscriptions WHERE openid = ? "
                "ORDER BY position", (openid,))]

    def set_topics(self, openid: str, topic_keys: list) -> list:
        """Replace the user's whole selection. The only way subscriptions change.

        Replace-not-append is what makes MAX_TOPICS safe: there is no sequence of
        calls that accumulates past the limit, and re-submitting the same list is
        idempotent. Duplicates are collapsed, order is preserved.
        """
        seen, keys = set(), []
        for k in topic_keys:
            k = (k or "").strip()
            if k and k not in seen:
                seen.add(k)
                keys.append(k)
        if not keys:
            raise ValueError("pick at least one topic")
        if len(keys) > MAX_TOPICS:
            raise ValueError(f"at most {MAX_TOPICS} topics (got {len(keys)})")

        with self._conn() as c:
            known = {r["topic_key"] for r in c.execute(
                "SELECT topic_key FROM custom_topics")}
            unknown = [k for k in keys if k.startswith(CUSTOM_PREFIX) and k not in known]
            if unknown:
                raise ValueError(f"unknown custom topic(s): {', '.join(unknown)}")
            c.execute("DELETE FROM subscriptions WHERE openid = ?", (openid,))
            c.executemany(
                "INSERT INTO subscriptions (openid, topic_key, position, created_at) "
                "VALUES (?, ?, ?, ?)",
                [(openid, k, i, _now()) for i, k in enumerate(keys)])
            c.execute("UPDATE users SET onboarded = 1 WHERE openid = ?", (openid,))
        return keys

    def subscriber_counts(self) -> dict:
        """{topic_key: active subscribers} — what to keep in the catalog."""
        with self._conn() as c:
            return {r["topic_key"]: r["n"] for r in c.execute(
                "SELECT s.topic_key, COUNT(*) AS n FROM subscriptions s "
                "JOIN users u ON u.openid = s.openid "
                "WHERE u.active = 1 GROUP BY s.topic_key ORDER BY n DESC")}

    def topics_in_use(self) -> list:
        """Distinct topics any active user subscribes to — the day's work queue.

        This is the whole cost story: work scales with distinct topics, not with
        users. Ten thousand people on the same three presets is three jobs.
        """
        with self._conn() as c:
            return [r["topic_key"] for r in c.execute(
                "SELECT DISTINCT s.topic_key FROM subscriptions s "
                "JOIN users u ON u.openid = s.openid WHERE u.active = 1")]

    # ------------------------------------------------------- custom topics
    @staticmethod
    def custom_key(spec: dict) -> str:
        """Content-addressed key: identical requests collapse onto one topic."""
        canon = json.dumps({"focus": (spec.get("focus") or "").strip().lower(),
                            "sources": spec.get("sources", [])},
                           sort_keys=True, ensure_ascii=False)
        return CUSTOM_PREFIX + hashlib.sha256(canon.encode()).hexdigest()[:16]

    def add_custom_topic(self, label: str, spec: dict, created_by: str = None) -> str:
        """Register a custom topic and return its key. Idempotent by content."""
        if not spec.get("sources"):
            raise ValueError("a custom topic needs at least one source")
        key = self.custom_key(spec)
        with self._conn() as c:
            c.execute("INSERT OR IGNORE INTO custom_topics "
                      "(topic_key, label, spec, created_by, created_at) "
                      "VALUES (?, ?, ?, ?, ?)",
                      (key, label, json.dumps(spec, ensure_ascii=False),
                       created_by, _now()))
        return key

    def get_custom_topic(self, key: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM custom_topics WHERE topic_key = ?",
                            (key,)).fetchone()
        if row is None:
            return None
        spec = json.loads(row["spec"])
        spec.update({"id": row["topic_key"], "title": row["label"]})
        return spec

    def prune_custom_topics(self) -> int:
        """Drop custom topics nobody subscribes to. Safe to run daily."""
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM custom_topics WHERE topic_key NOT IN "
                "(SELECT topic_key FROM subscriptions)")
        return cur.rowcount

    # -------------------------------------------------------------- renders
    def get_render(self, topic_key: str, day: str = None) -> list | None:
        """Today's items for a topic, or None if it hasn't been built yet."""
        with self._conn() as c:
            row = c.execute("SELECT items FROM renders WHERE topic_key = ? AND day = ?",
                            (topic_key, day or today())).fetchone()
        return json.loads(row["items"]) if row else None

    def put_render(self, topic_key: str, items: list, day: str = None):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO renders "
                      "(topic_key, day, items, built_at) VALUES (?, ?, ?, ?)",
                      (topic_key, day or today(),
                       json.dumps(items, ensure_ascii=False), _now()))

    def purge_renders(self, keep_days: int = 14) -> int:
        cutoff = (dt.date.today() - dt.timedelta(days=keep_days)).isoformat()
        with self._conn() as c:
            cur = c.execute("DELETE FROM renders WHERE day < ?", (cutoff,))
        return cur.rowcount

    # ----------------------------------------------------------- deliveries
    def mark_delivery(self, openid: str, status: str, detail: str = None,
                      day: str = None):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO deliveries "
                      "(openid, day, status, detail, updated_at) VALUES (?, ?, ?, ?, ?)",
                      (openid, day or today(), status, detail, _now()))

    def delivered_today(self, openid: str, day: str = None) -> bool:
        """Idempotency guard — a retried batch must not double-send."""
        with self._conn() as c:
            row = c.execute("SELECT status FROM deliveries WHERE openid = ? AND day = ?",
                            (openid, day or today())).fetchone()
        return bool(row) and row["status"] == "sent"

    def delivery_stats(self, day: str = None) -> dict:
        with self._conn() as c:
            return {r["status"]: r["n"] for r in c.execute(
                "SELECT status, COUNT(*) AS n FROM deliveries WHERE day = ? "
                "GROUP BY status", (day or today(),))}


# --------------------------------------------------------------------------
# Catalog — presets, loaded from YAML, merged with custom topics on lookup
# --------------------------------------------------------------------------
class Catalog:
    """Read-only view of catalog.yaml plus the store's custom topics."""

    def __init__(self, path="catalog.yaml", store: Store = None):
        import yaml
        data = yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8")) or {}
        self.groups = data.get("groups", [])
        self.presets = {p["key"]: p for p in data.get("presets", [])}
        self.store = store

    def list_for_picker(self) -> list:
        """Grouped preset list for the onboarding UI."""
        by_group = {g["key"]: {"key": g["key"], "label": g["label"], "topics": []}
                    for g in self.groups}
        for p in self.presets.values():
            g = by_group.setdefault(p.get("group", "other"),
                                    {"key": p.get("group", "other"),
                                     "label": "其他", "topics": []})
            g["topics"].append({"key": p["key"], "label": p["label"],
                                "blurb": p.get("blurb", "")})
        return [g for g in by_group.values() if g["topics"]]

    def resolve(self, topic_key: str) -> dict | None:
        """Topic key -> a spec the pipeline can run. None if it no longer exists."""
        if topic_key.startswith(CUSTOM_PREFIX):
            return self.store.get_custom_topic(topic_key) if self.store else None
        p = self.presets.get(topic_key)
        if p is None:
            return None
        spec = {k: v for k, v in p.items()
                if k not in ("key", "group", "label", "blurb")}
        spec.update({"id": p["key"], "title": p["label"]})
        return spec

    def label(self, topic_key: str) -> str:
        p = self.presets.get(topic_key)
        if p:
            return p["label"]
        custom = self.store.get_custom_topic(topic_key) if self.store else None
        return custom["title"] if custom else topic_key
