# Deploying to WeChat

## The daily loop

The mini program does not update itself. It asks your backend for today's data
every time it opens; the content changes because a scheduled job rewrote it
overnight.

```
06:00  cron: python service.py build --send
       └─ each distinct topic fetched + summarized ONCE -> renders table

08:00  official account broadcasts one generic message to everyone
       └─ with a mini-program card attached

user taps the card
       └─ mini program opens -> wx.login() -> your API resolves the openid
          -> new user? show the picker.  returning user? show their digest
          -> GET /api/digest reads rows the 06:00 job already wrote
```

The split matters: **push cadence comes from the official account,
personalization comes from the mini program.** A subscription account can
broadcast daily but only sends everyone the same thing; a mini program can show
each user their own three topics but can't push daily. Together neither limit
bites.

Two implications worth internalizing:

- The broadcast is a *nudge*, not content. Something like "今天的 digest 已更新"
  plus a card. Do not try to put personalized content in it.
- `/api/digest` is a pure read. Ten thousand people opening the app at 8am costs
  database reads, not model calls, because the work happened at 06:00.

---

## Why Cloud Run

Running the container on **微信云托管 (WeChat Cloud Run)** and calling it with
`wx.cloud.callContainer` skips the three things that otherwise gate launch:
ICP filing (备案), an HTTPS domain, and the request-domain whitelist. WeChat
routes the call over its own protocol, so there is no public domain involved.

It also injects `X-WX-OPENID` on every request, so there is no login exchange to
implement, and it signs calls to `api.weixin.qq.com`, so there is no access
token to cache. `wechat.py` uses both when `WX_CLOUD=1` and falls back to
`code2session` + token caching everywhere else.

Self-hosting works too — you'll need 备案, a certificate, and the domain
whitelist, which is weeks of lead time rather than an afternoon.

## Deploy

```bash
# 1. push the image (or point Cloud Run at the repo and let it build)
docker build -t open-digest .
```

In the Cloud Run console:

**Environment variables**

| key | value |
|---|---|
| `WX_CLOUD` | `1` |
| `OPENROUTER_API_KEY` | your key |
| `DIGEST_DB` | `/data/digest.db` |
| `DIGEST_TITLE` | e.g. `每日 Digest` |

**Storage.** Mount a volume at `/data`. Cloud Run containers have an ephemeral
filesystem — without a volume every user's subscriptions are erased on each
redeploy. When SQLite stops being enough, point `DIGEST_DB` at a managed
database; only `store.py` changes.

**Scheduled job.** One daily trigger:

```
0 6 * * *   python service.py build --send
```

Idempotent by design — topics built today come from cache and users already
marked `sent` are skipped, so a retry after a crash costs nothing and
double-sends nothing.

**Health check.** `GET /health`, already in the Dockerfile.

## Mini program client

```js
// app.js — one call on launch decides which page to show
const call = (path, data = {}, method = 'POST') =>
  wx.cloud.callContainer({
    config: { env: 'your-cloud-env-id' },
    path,
    method,
    header: { 'X-WX-SERVICE': 'open-digest', 'content-type': 'application/json' },
    data,
  }).then(r => r.data)

App({
  async onLaunch() {
    const s = await call('/api/session')
    // s = { ok, new_user, topics: [...], max_topics: 3, language }
    wx.reLaunch({ url: s.new_user ? '/pages/picker/index' : '/pages/digest/index' })
  },
})
```

```js
// pages/picker/index.js — onboarding
const { groups, max_topics } = await call('/api/catalog', {}, 'GET')
// groups: [{ key, label, topics: [{ key, label, blurb }] }]

// user taps up to max_topics, then:
const r = await call('/api/topics', { topics: ['ai', 'macro', 'jobs_newgrad'] })
// over the limit -> { ok: false, error: 'at most 3 topics (got 4)' }
// enforce it in the UI too, but the server is the one that decides
```

```js
// pages/digest/index.js — the daily read
const { digest } = await call('/api/digest', {}, 'GET')
// digest === null  ->  "今天还没生成，稍后再来"
// otherwise: { date, title, sections: [{ title, items: [{title, summary, url}] }] }
```

Custom topics are two steps on purpose — create, preview, then subscribe — so an
abandoned preview leaves nothing behind:

```js
const { topic } = await call('/api/topics/custom', { description: '苹果和中国船舶的股价走势' })
// topic = { key: 'custom:a1b2…', label: '股票', focus: '…', preview: 'news: 苹果 财报, …' }
// show topic.preview for confirmation, then:
await call('/api/topics', { topics: ['ai', topic.key] })
```

## API

| method | path | notes |
|---|---|---|
| `GET` | `/health` | liveness |
| `POST` | `/api/session` | new-user vs returning; call on every launch |
| `GET` | `/api/catalog` | the picker, grouped. Public. |
| `POST` | `/api/topics` | `{topics:[...]}`, at most 3, replaces the set |
| `POST` | `/api/topics/custom` | free text -> topic key. Rate-limited, moderated. |
| `GET` | `/api/digest` | today's digest. `?format=html`, `?day=YYYY-MM-DD` |
| `POST` | `/api/unsubscribe` | `{active:false}` — pauses without losing topics |

Everything except `/health` and `/api/catalog` requires an openid.

## Content moderation

`/api/topics/custom` runs the description through `msg_sec_check` before it is
stored, because a custom topic's label and description are user-generated
content that gets displayed.

**It fails closed.** If moderation is unreachable, the request is rejected.
Shipping unmoderated user text because a dependency was down is the failure mode
that gets a mini program pulled.

Worth deciding before launch: summaries are model-generated text derived from
fetched news, which is not strictly UGC but is also not content you wrote. Check
the current rules for your category.

## Before you register

Both of these gate registration, not implementation — check them first.

**Category.** Individual-entity mini programs are limited to a narrow category
set. News and information categories generally require an enterprise entity plus
an 互联网新闻信息服务许可证, and financial content carries its own qualification
requirements. That directly affects whether you can ship the `a_shares` and
`crypto` presets — it may be simplest to launch without them and add them if and
when the entity supports it.

**Push.** Long-term subscription messages are open only to specific categories
(government services, medical, transport, finance, education); a reading digest
isn't one. `wechat.send_subscribe_message` sends one-time subscription messages,
where each user authorization buys exactly one push. Prompting for several
authorizations at once buys a few days before the user has to return.

These rules change. Verify against the official docs before committing.

Sources: [云托管免备案](https://developers.weixin.qq.com/community/minihome/doc/000a40427b40502844244c17866000) ·
[小程序访问云托管](https://developers.weixin.qq.com/minigame/dev/wxcloudrun/src/development/call/mini.html) ·
[公众号关联小程序](https://developers.weixin.qq.com/community/business/doc/000642879a02c8076972860ca66c0d) ·
[订阅号群发规则](https://developers.weixin.qq.com/doc/subscription/guide/product/message/Batch_Sends.html) ·
[长期订阅消息类目](https://developers.weixin.qq.com/community/develop/article/doc/00086293004d006da5ee7518c56813)
