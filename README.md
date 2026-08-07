# open-digest

A daily digest of whatever you actually care about, delivered to your inbox.

You describe your topics in plain language. It searches, filters, summarizes with
an LLM, and emails you the result. No hardcoded categories — the pipeline is
driven entirely by your config file.

```yaml
topics:
  - title: "Sourdough"
    focus: >
      Technique posts about hydration, fermentation and shaping.
      Recipes and results only; skip equipment reviews.
    sources:
      - type: news
        queries: ["sourdough technique", "bread baking science"]
      - type: reddit
        subs: ["Sourdough"]
```

That's a section of your digest. Add another block, get another section.

---

## Quick start

```bash
git clone https://github.com/YOUR_USERNAME/open-digest
cd open-digest
pip install -r requirements.txt

python wizard.py            # -> digest.yaml
python main.py --print      # dry run: builds it, prints it, sends nothing
```

The wizard asks for an [OpenRouter key](https://openrouter.ai/keys) first, then
one sentence per section:

```
Topic 1: 苹果和中国船舶的股价走势、财报和分析师观点
  working…
  ✓ 股票  news: 苹果 财报, Apple stock earnings, 中国船舶 股价… · quotes for AAPL, 600150.SS
    Keep it? [Y/n]
```

It works out the search queries, picks the sources, writes the summarizer prompt,
and recognizes that you named two companies so it adds a quotes table. Two more
questions after that and you're done. `python wizard.py --manual` fills everything
in by hand instead.

`--print` costs about a cent and sends no email. Run it until the output looks
right, then drop the flag. `digest.yaml` is plain YAML — editing `focus` and
`queries` by hand is the fastest way to correct anything the wizard got wrong.

## How it works

```
digest.yaml ──> sources.py ──> summarizer.py ──> render.py ──> your inbox
  topics        fetch & dedupe   one LLM call     HTML +          (or Slack,
                per topic        per topic        JSON archive     or a file)
```

Four moving parts, each independently replaceable:

| File | Does |
|---|---|
| `sources.py` | Every content source. Add a function, register it, done. |
| `summarizer.py` | Builds the prompt and JSON schema *from your topics*. |
| `render.py` | HTML email + plain text + delivery. |
| `main.py` | Wires them together. |
| `wizard.py` | Setup only. Nothing at runtime depends on it. |

Every run also writes `data/YYYY-MM-DD.json` — the digest as structured data,
if you want to build something else on top of it.

## Writing a good topic

Three fields do the work.

**`focus`** is the one that matters. It goes straight into the prompt and decides
what gets picked and what gets dropped. Be specific about exclusions:

```yaml
# vague — you'll get whatever the sources happened to publish
focus: climate news

# specific — the model can actually filter on this
focus: >
  Funding rounds, pilot deployments and regulatory decisions in carbon removal.
  Skip op-eds, conference announcements, and anything paywalled.
```

**`sources`** decide what the model gets to choose *from*. A tight `focus` can't
rescue a feed full of irrelevant items — and the model can't select something
that was never fetched.

**`summary_style`** controls the writing. One-liners scan fast; 2-4 sentences
mean you rarely need to click through.

```yaml
- id: carbon
  title: "Carbon Removal"
  focus: >
    Funding rounds, pilot deployments and regulatory decisions.
    Skip op-eds and conference announcements.
  max_items: 5
  summary_style: >
    Each summary is 2-3 sentences focused on the concrete numbers, companies
    and dates reported in the source.
  sources:
    - type: news
      queries: ["direct air capture", "carbon removal funding"]
      window_days: 3
    - type: rss
      feeds: ["https://www.canarymedia.com/feed"]
```

## Sources

| type | Use for | Key options |
|---|---|---|
| `news` | anything — Google News over your keywords | `queries`, `window_days`, `per_query` |
| `rss` | blogs, newsletters, official feeds | `feeds`, `keywords`, `per_feed` |
| `reddit` | community discussion | `subs`, `sort`, `limit` |
| `hackernews` | tech stories above a score threshold | `keywords`, `min_score` |
| `arxiv` | preprints | `categories` or `query`, `max_results` |
| `json_feed` | any public JSON endpoint | `url`, `fields`, `keywords`, `recent_days` |
| `tickers` | quotes + headlines (table, no LLM) | `symbols`, `news_per_ticker` |

Full option reference: [SOURCES.md](SOURCES.md). None of these need an API key.

Start with `news` — it turns any keyword into a feed and works for topics that
have no dedicated source. Add `rss` once you know which publications you trust.

**Two section types skip the LLM entirely** and render as widgets:

```yaml
- id: markets
  title: "Portfolio"
  kind: tickers
  symbols: ["NVDA", "BTC-USD"]

- id: dates
  title: "Deadlines"
  kind: countdown
  deadlines:
    - { name: "Grant application", date: "2026-10-20" }
```

## Delivery

```yaml
delivery:
  method: resend           # email — free tier is 100/day
  to: you@example.com
  from: onboarding@resend.dev
```

Without a domain, Resend's `onboarding@resend.dev` only delivers to the address
you signed up with — fine for a personal digest.

Other options: `method: file` writes `data/preview.html` and needs no key.
`method: webhook` POSTs to Slack, Discord, or your own endpoint.

## Daily automation

`.github/workflows/digest.yml` runs on GitHub Actions — free, no server.

1. Push this repo to your account
2. Settings → Secrets and variables → Actions, add:
   - `OPENROUTER_API_KEY`
   - `RESEND_API_KEY` (if emailing)
   - `DIGEST_CONFIG` — paste your entire `digest.yaml`
3. Actions tab → "Digest" → Run workflow, to test it
4. Edit the `cron` line for your timezone (it's UTC)

`digest.yaml` is gitignored, so your keywords and email address stay out of a
public repo. That's why the config comes from a secret. If your config isn't
sensitive, drop it from `.gitignore` and delete the "Write config" step.

## Cost

One run is one LLM call per topic. Five topics on DeepSeek V4 Pro is roughly
$0.01/day — about **$4/year**. Same digest on Claude Fable 5 is closer to $150/year.

The model is one line of config:

```yaml
llm:
  model: deepseek/deepseek-v4-pro
  fallback_model: anthropic/claude-fable-5   # only used when the main model fails
```

Any [OpenRouter model](https://openrouter.ai/models) id works. One key, every provider.

## Reliability

The failure mode that matters is an LLM emitting something that isn't the JSON
you asked for. Three layers, strongest first:

1. **Structured outputs** (`json_schema`) at the API — auto-degrades to
   `json_object` then unconstrained for models that don't support it
2. **The prompt** states the exact shape
3. **Exit validation** in `summarizer.py` — the only layer fully under our
   control. Strips code fences, repairs near-JSON, drops items missing a
   title/summary/url, **and drops any item whose URL wasn't in the source
   material.** That last check is the anti-hallucination guard: a model can
   invent a plausible headline, but it can't invent a URL that's in the input.

Invalid output retries once, then falls back to `fallback_model`, then skips the
topic. Sources are equally defensive: a fetch failure returns `[]` with a warning
rather than raising, so one dead feed can't take down the digest.

## Adding a source

```python
# sources.py
def mastodon(instance, hashtag, limit=20):
    r = _get(f"https://{instance}/api/v1/timelines/tag/{hashtag}?limit={limit}")
    if not r:
        return []                      # never raise — one dead source, not a dead digest
    return [{"source": f"@{instance}",
             "title": _strip_html(p.get("content"), 120),
             "summary": _strip_html(p.get("content")),
             "url": p.get("url", "")} for p in r.json()]

REGISTRY["mastodon"] = mastodon
```

```yaml
sources:
  - type: mastodon
    instance: mastodon.social
    hashtag: rustlang
```

Config keys map to function arguments. Nothing else needs to change.

## Serving more than one person

`service.py` turns this into a multi-user product: a preset topic catalog, up to
three subscriptions per user, and a nightly batch.

```bash
python service.py demo --user wx_test   # onboard a fake user end to end
python service.py build                 # today's batch, one HTML per user
python service.py stats                 # subscribers per topic
```

Each topic is summarized **once per day for everyone who subscribes to it**, so
cost tracks distinct topics rather than user count — 10,000 users sharing three
presets is three LLM calls. Users who want something not in the catalog describe
it in a sentence and get a content-addressed custom topic, shared with anyone who
asks for the same thing.

`api.py` puts an HTTP layer on top for a mobile client — session, catalog,
topic selection, and today's digest. The read path never calls a model, so
traffic costs database reads rather than API spend.

See [SERVICE.md](SERVICE.md) for the schema and guarantees, and
[DEPLOY.md](DEPLOY.md) for shipping it as a WeChat mini program.

## Requirements

Python 3.9+, an [OpenRouter](https://openrouter.ai) key. A [Resend](https://resend.com)
key if you want email.

## License

MIT
