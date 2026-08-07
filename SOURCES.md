# Source reference

Every key under a source block maps directly to a function argument in
`sources.py`. Unknown types and bad arguments print a warning and return nothing
— they never crash the run.

---

## `news` — Google News search

The general-purpose source. Any keyword becomes a feed, which makes it the right
starting point for topics with no dedicated publication.

```yaml
- type: news
  queries: ["carbon capture", "direct air capture funding"]
  per_query: 6          # max items per query        (default 6)
  window_days: 2        # only results this recent    (default 2)
  lang: en-US           # interface language          (default en-US)
  country: US           # edition                     (default US)
```

Google News operators work inside a query:

```yaml
queries:
  - '"quantum computing" site:nature.com'
  - 'housing policy -opinion'
  - 'intitle:earnings semiconductor'
```

Non-English editions:

```yaml
- type: news
  queries: ["人工智能 监管"]
  lang: zh-CN
  country: CN
```

---

## `rss` — any RSS 2.0 or Atom feed

```yaml
- type: rss
  feeds:
    - https://openai.com/blog/rss.xml
    - https://www.canarymedia.com/feed
  per_feed: 8                        # default 8
  keywords: ["policy", "regulation"] # optional pre-filter on title+summary
```

`keywords` narrows a broad feed before the LLM sees it — useful when a
publication covers far more than your topic. Omit it to pass everything through.

---

## `reddit` — public JSON, no key

```yaml
- type: reddit
  subs: ["MachineLearning", "LocalLLaMA"]
  sort: hot          # hot | new | top | rising   (default hot)
  limit: 8           # per subreddit              (default 8)
  body_chars: 500    # post text truncation       (default 500)
```

Reddit rate-limits aggressively. The fetcher tries `www` JSON, then `old` JSON,
then RSS, sleeping between subreddits. If all three fail you get a warning and an
empty list. Keep `subs` short.

---

## `hackernews` — Algolia search over HN

```yaml
- type: hackernews
  keywords: ["rust", "compiler"]
  min_score: 50      # points threshold       (default 50)
  window_days: 1     # lookback               (default 1)
  limit: 30          # total after ranking    (default 30)
  tags: story        # story | comment | show_hn | ask_hn
```

`min_score` is the main dial. Below ~30 you get noise; above ~150 you'll often
get nothing on a quiet day.

---

## `arxiv` — preprints

```yaml
# by category
- type: arxiv
  categories: ["cs.CL", "cs.LG"]
  max_results: 15
  abstract_chars: 600

# by free-text query — any arXiv field
- type: arxiv
  query:
    - 'all:"retrieval augmented generation"'
    - 'au:Hinton AND cat:cs.LG'
    - 'ti:"diffusion model" AND abs:video'
```

Prefixes: `ti:` title, `abs:` abstract, `au:` author, `cat:` category, `all:`
everything. Combine with `AND` / `OR` / `ANDNOT`. Both keys can be used together;
each query is a separate API call with a 3-second pause, so keep the list short.

---

## `json_feed` — any public JSON endpoint

The escape hatch. If a site publishes JSON, this turns it into digest items.

```yaml
- type: json_feed
  url: https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json
  item_path: ""              # dotted path to the array; "" if the body is the array
  fields:
    title: "{company_name} — {title}"   # template, or a bare key like "title"
    summary: "{locations} · {sponsorship}"
    url: "url"
  date_field: date_posted    # epoch seconds or ISO 8601
  recent_days: 3             # drop anything older
  keywords: ["software"]     # filter on the rendered title+summary
  limit: 40
```

**`fields`** maps their shape onto ours. A bare string copies one key; a string
containing `{braces}` is a template. List values are joined with commas.

**`item_path`** digs into a wrapper: `data.items` for
`{"data": {"items": [...]}}`. Leave it empty when the response is already an array.

**`date_field` + `recent_days`** must be used together. Items whose date can't be
parsed are dropped when filtering is on.

---

## `tickers` — quotes and headlines

A widget, not a topic: it renders as a table and never reaches the LLM. Declare
it with `kind: tickers` at the section level rather than inside `sources`.

```yaml
- id: markets
  title: "Portfolio"
  kind: tickers
  symbols: ["NVDA", "AAPL", "BTC-USD", "^GSPC"]
  news_per_ticker: 2
```

Yahoo Finance symbols: `BTC-USD` crypto, `^GSPC` indices, `EURUSD=X` forex,
`7203.T` international listings.

---

## `countdown` — days until a date

Also a widget. No fetching at all.

```yaml
- id: dates
  title: "Deadlines"
  kind: countdown
  deadlines:
    - { name: "Grant application", date: "2026-10-20" }
    - { name: "Conference abstract", date: "2026-11-30" }
```

Dates in the past are hidden automatically. Under 30 days renders in red.

---

## Writing your own

```python
# sources.py
def lobsters(tag=None, limit=20):
    url = f"https://lobste.rs/t/{tag}.json" if tag else "https://lobste.rs/hottest.json"
    r = _get(url)
    if not r:
        return []                        # the contract: never raise
    return [{"source": "lobste.rs",
             "title": s.get("title", ""),
             "summary": f"{s.get('score', 0)} points",
             "url": s.get("url") or s.get("short_id_url", "")}
            for s in r.json()[:limit]]

REGISTRY["lobsters"] = lobsters
```

Two rules:

1. Return `list[dict]` with `source`, `title`, `summary`, `url`. Items without a
   `url` are dropped during de-duplication.
2. Never raise. Catch, print `[warn] ...`, return `[]`.

Helpers available: `_get(url, headers=...)` returns a response or `None`;
`_strip_html(s, limit)` cleans HTML; `_as_list(v)` accepts a scalar or a list so
users can write either in YAML.
