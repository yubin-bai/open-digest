"""Generic content sources.

Every source is a plain function registered in ``REGISTRY`` under a type name.
A source receives its own keyword args straight from the YAML config and returns
``list[dict]`` with at least ``{source, title, summary, url}``.

Contract: a source NEVER raises. On failure it returns ``[]`` and prints a
warning, so one dead feed can't take down the whole digest.

Adding a new source type = write a function + register it. Nothing else in the
codebase needs to know it exists.
"""

from __future__ import annotations

import datetime as dt
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET

import requests

UA = {"User-Agent": "open-digest-bot/1.0 (+https://github.com/open-digest)"}
BROWSER_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}


def _get(url, headers=None, timeout=20, **kw):
    try:
        r = requests.get(url, headers=headers or UA, timeout=timeout, **kw)
        r.raise_for_status()
        return r
    except Exception as e:  # noqa: BLE001 - deliberate catch-all, see module docstring
        print(f"   [warn] fetch failed: {url} ({e})")
        return None


def _strip_html(s: str, limit: int = 400) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()[:limit]


def _as_list(v) -> list:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


# --------------------------------------------------------------------------
# news — Google News RSS. The workhorse: turns ANY keyword into a feed.
# --------------------------------------------------------------------------
def news(queries, per_query: int = 6, window_days: int = 2, lang: str = "en-US",
         country: str = "US"):
    """Google News search RSS. `queries` are plain search strings.

    Supports Google News operators, e.g. ``"climate policy" site:reuters.com``.
    """
    out, seen = [], set()
    for q in _as_list(queries):
        scoped = f"{q} when:{window_days}d"
        url = ("https://news.google.com/rss/search?q="
               f"{urllib.parse.quote(scoped)}"
               f"&hl={lang}&gl={country}&ceid={country}:{lang.split('-')[0]}")
        r = _get(url)
        if not r:
            continue
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError:
            continue
        for item in list(root.iter("item"))[:per_query]:
            link = (item.findtext("link") or "").strip()
            if not link or link in seen:
                continue
            seen.add(link)
            out.append({
                "source": f"news:{q}",
                "title": (item.findtext("title") or "").strip(),
                "summary": _strip_html(item.findtext("description")),
                "url": link,
                "published": (item.findtext("pubDate") or "").strip(),
            })
    return out


# --------------------------------------------------------------------------
# rss — any RSS 2.0 / Atom feed
# --------------------------------------------------------------------------
def rss(feeds, per_feed: int = 8, keywords=None):
    """Parse arbitrary RSS/Atom feeds. Optional `keywords` filter on title."""
    kws = [k.lower() for k in _as_list(keywords)]
    out = []
    for feed in _as_list(feeds):
        r = _get(feed)
        if not r:
            continue
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError:
            print(f"   [warn] not valid XML: {feed}")
            continue
        host = urllib.parse.urlparse(feed).netloc or feed
        items = []
        for item in root.iter("item"):  # RSS 2.0
            items.append({
                "source": host,
                "title": (item.findtext("title") or "").strip(),
                "summary": _strip_html(item.findtext("description")),
                "url": (item.findtext("link") or "").strip(),
                "published": (item.findtext("pubDate") or "").strip(),
            })
        for entry in root.findall("a:entry", ATOM_NS):  # Atom
            link = entry.find("a:link", ATOM_NS)
            items.append({
                "source": host,
                "title": entry.findtext("a:title", "", ATOM_NS).strip(),
                "summary": _strip_html(
                    entry.findtext("a:summary", "", ATOM_NS)
                    or entry.findtext("a:content", "", ATOM_NS)),
                "url": link.get("href") if link is not None else "",
                "published": entry.findtext("a:updated", "", ATOM_NS).strip(),
            })
        if kws:
            items = [i for i in items
                     if any(k in (i["title"] + i["summary"]).lower() for k in kws)]
        out.extend(i for i in items[:per_feed] if i["url"])
    return out


# --------------------------------------------------------------------------
# arxiv — free-text search over any arXiv field, not just categories
# --------------------------------------------------------------------------
def arxiv(query=None, categories=None, max_results: int = 15, abstract_chars: int = 600):
    """arXiv API.

    `query`      raw arXiv search_query, e.g. ``all:"diffusion model" AND cat:cs.CV``
    `categories` shorthand that expands to ``cat:X``, kept for simple configs.
    Provide either or both.
    """
    queries = [q for q in _as_list(query) if q]
    queries += [f"cat:{c}" for c in _as_list(categories)]
    if not queries:
        return []
    papers, seen = [], set()
    for i, q in enumerate(queries):
        if i:
            time.sleep(3)  # arXiv asks for >=3s between calls
        url = ("http://export.arxiv.org/api/query?search_query="
               f"{urllib.parse.quote(q)}"
               f"&sortBy=submittedDate&sortOrder=descending&max_results={max_results}")
        r = _get(url, timeout=30)
        if not r:
            continue
        try:
            root = ET.fromstring(r.text)
        except ET.ParseError:
            continue
        for entry in root.findall("a:entry", ATOM_NS):
            eid = entry.findtext("a:id", "", ATOM_NS).strip()
            if not eid or eid in seen:
                continue
            seen.add(eid)
            papers.append({
                "source": f"arXiv ({q})",
                "title": entry.findtext("a:title", "", ATOM_NS).strip().replace("\n", " "),
                "summary": entry.findtext("a:summary", "", ATOM_NS).strip()[:abstract_chars],
                "url": eid,
                "published": entry.findtext("a:published", "", ATOM_NS).strip(),
            })
    return papers


# --------------------------------------------------------------------------
# hackernews — Algolia search over HN
# --------------------------------------------------------------------------
def hackernews(keywords, min_score: int = 50, window_days: int = 1, limit: int = 30,
               tags: str = "story"):
    since = int(time.time()) - window_days * 86400
    items, seen = [], set()
    for kw in _as_list(keywords):
        url = ("https://hn.algolia.com/api/v1/search?query="
               f"{urllib.parse.quote(str(kw))}&tags={tags}"
               f"&numericFilters=points>{min_score},created_at_i>{since}")
        r = _get(url)
        if not r:
            continue
        try:
            hits = r.json().get("hits", [])
        except Exception:  # noqa: BLE001
            continue
        for hit in hits:
            oid = hit.get("objectID")
            if not oid or oid in seen:
                continue
            seen.add(oid)
            items.append({
                "source": "HackerNews",
                "title": hit.get("title") or hit.get("story_title") or "",
                "summary": f"{hit.get('points', 0)} points, "
                           f"{hit.get('num_comments', 0)} comments",
                "url": hit.get("url") or f"https://news.ycombinator.com/item?id={oid}",
                "_score": hit.get("points", 0),
            })
    items.sort(key=lambda x: x["_score"], reverse=True)
    for i in items:
        i.pop("_score", None)
    return items[:limit]


# --------------------------------------------------------------------------
# reddit — public JSON, no API key
# --------------------------------------------------------------------------
def reddit(subs, limit: int = 8, sort: str = "hot", body_chars: int = 500):
    """Reddit public endpoints. Tries www JSON -> old JSON -> RSS before giving up."""
    posts = []
    for i, sub in enumerate(_as_list(subs)):
        if i:
            time.sleep(3)  # consecutive hits get 429'd
        data = None
        for host in ("www", "old"):
            r = _get(f"https://{host}.reddit.com/r/{sub}/{sort}.json?limit={limit}",
                     headers=BROWSER_UA)
            if r:
                try:
                    data = r.json()
                    break
                except Exception:  # noqa: BLE001
                    data = None
            time.sleep(1)
        if data:
            for child in data.get("data", {}).get("children", []):
                d = child.get("data", {})
                if d.get("stickied"):
                    continue
                posts.append({
                    "source": f"r/{sub}",
                    "title": d.get("title", ""),
                    "summary": (d.get("selftext") or "")[:body_chars]
                               or f"{d.get('score', 0)} upvotes, "
                                  f"{d.get('num_comments', 0)} comments",
                    "url": "https://reddit.com" + d.get("permalink", ""),
                })
            continue
        r = _get(f"https://old.reddit.com/r/{sub}/{sort}/.rss?limit={limit}",
                 headers=BROWSER_UA)
        if not r:
            continue
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError:
            continue
        for entry in root.findall("a:entry", ATOM_NS):
            link = entry.find("a:link", ATOM_NS)
            posts.append({
                "source": f"r/{sub}",
                "title": entry.findtext("a:title", "", ATOM_NS).strip(),
                "summary": "",
                "url": link.get("href") if link is not None else "",
            })
    return posts


# --------------------------------------------------------------------------
# json_feed — any public JSON array/object, mapped to our shape by config
# --------------------------------------------------------------------------
def json_feed(url, item_path: str = "", fields=None, keywords=None,
              recent_days=None, date_field: str = "", limit: int = 40):
    """Pull an arbitrary public JSON endpoint into digest items.

    `item_path`   dotted path to the array, "" if the body IS the array
    `fields`      map of {title,summary,url} -> source key, or a "{a} - {b}" template
    `keywords`    keep only items whose rendered title/summary matches one
    `recent_days` + `date_field`  drop items older than N days (epoch seconds or ISO)

    Example — GitHub job listings::

        type: json_feed
        url: https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json
        fields: {title: "{company_name} — {title}", summary: "{locations}", url: "url"}
        date_field: date_posted
        recent_days: 2
    """
    fields = fields or {"title": "title", "summary": "summary", "url": "url"}
    r = _get(url)
    if not r:
        return []
    try:
        data = r.json()
    except Exception as e:  # noqa: BLE001
        print(f"   [warn] not valid JSON: {url} ({e})")
        return []
    for part in [p for p in item_path.split(".") if p]:
        data = (data or {}).get(part) if isinstance(data, dict) else None
    if isinstance(data, dict):
        data = list(data.values())
    if not isinstance(data, list):
        print(f"   [warn] no array found at item_path={item_path!r} in {url}")
        return []

    kws = [k.lower() for k in _as_list(keywords)]
    cutoff = time.time() - recent_days * 86400 if recent_days else None
    host = urllib.parse.urlparse(url).netloc
    out = []
    for row in data:
        if not isinstance(row, dict):
            continue
        if cutoff is not None and date_field:
            ts = _epoch(row.get(date_field))
            if ts is None or ts < cutoff:  # unparseable dates are dropped too
                continue
        item = {k: _render(tpl, row) for k, tpl in fields.items()}
        if not item.get("url") or not item.get("title"):
            continue
        blob = f"{item.get('title', '')} {item.get('summary', '')}".lower()
        if kws and not any(k in blob for k in kws):
            continue
        item.setdefault("summary", "")
        item["source"] = host
        out.append(item)
        if len(out) >= limit:
            break
    return out


def _render(tpl, row: dict) -> str:
    """"{a} — {b}" -> formatted; "a" -> row["a"]. Lists become comma joins."""
    def flat(v):
        if isinstance(v, list):
            return ", ".join(str(x) for x in v)
        return "" if v is None else str(v)

    if "{" in tpl:
        return re.sub(r"\{(\w+)\}", lambda m: flat(row.get(m.group(1), "")), tpl).strip(" —-,")
    return flat(row.get(tpl, ""))


def _epoch(v):
    if isinstance(v, (int, float)):
        return float(v) if v > 1e6 else None
    if isinstance(v, str):
        for parse in (lambda s: dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp(),
                      lambda s: float(s)):
            try:
                return parse(v)
            except Exception:  # noqa: BLE001
                continue
    return None


# --------------------------------------------------------------------------
# tickers — quotes + headlines, rendered as a table instead of prose
# --------------------------------------------------------------------------
def tickers(symbols, news_per_ticker: int = 2):
    """Yahoo Finance quotes + Google News headlines. Key-free.

    Returns rows shaped for the table renderer, not for the LLM.
    """
    out = []
    for t in _as_list(symbols):
        item = {"ticker": t, "close": None, "change_pct": None, "currency": "", "news": []}
        r = _get(f"https://query1.finance.yahoo.com/v8/finance/chart/{t}"
                 "?range=2d&interval=1d", headers=BROWSER_UA)
        if r:
            try:
                meta = r.json()["chart"]["result"][0]["meta"]
                price = meta.get("regularMarketPrice")
                prev = meta.get("chartPreviousClose") or meta.get("previousClose")
                item["close"] = round(price, 2) if price is not None else None
                item["currency"] = meta.get("currency", "")
                if price and prev:
                    item["change_pct"] = round((price - prev) / prev * 100, 2)
            except Exception as e:  # noqa: BLE001
                print(f"   [warn] quote parse failed: {t} ({e})")
        for n in news([f"{t} stock"], per_query=news_per_ticker, window_days=1):
            item["news"].append({"title": n["title"], "url": n["url"]})
        if item["close"] is not None or item["news"]:
            out.append(item)
    return out


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------
REGISTRY = {
    "news": news,
    "rss": rss,
    "arxiv": arxiv,
    "hackernews": hackernews,
    "reddit": reddit,
    "json_feed": json_feed,
    "tickers": tickers,
}

#: source types whose output is rendered directly, never sent to the LLM
RAW_TYPES = {"tickers"}


def fetch(spec: dict) -> list:
    """Run one source spec (``{type: news, queries: [...]}``) from the config."""
    spec = dict(spec)
    kind = spec.pop("type", None)
    fn = REGISTRY.get(kind)
    if fn is None:
        print(f"   [warn] unknown source type {kind!r}, "
              f"known: {', '.join(sorted(REGISTRY))}")
        return []
    try:
        return fn(**spec) or []
    except TypeError as e:
        print(f"   [warn] bad config for source {kind!r}: {e}")
        return []
    except Exception as e:  # noqa: BLE001
        print(f"   [warn] source {kind!r} failed: {e}")
        return []


def countdown(deadlines) -> list:
    """Optional dated-reminder widget: [{name, date}] -> adds days_left, sorted."""
    today = dt.date.today()
    out = []
    for d in _as_list(deadlines):
        try:
            days = (dt.date.fromisoformat(str(d["date"])) - today).days
        except Exception:  # noqa: BLE001
            continue
        if days >= 0:
            out.append({"name": d.get("name", "?"), "date": str(d["date"]),
                        "days_left": days})
    out.sort(key=lambda x: x["days_left"])
    return out
