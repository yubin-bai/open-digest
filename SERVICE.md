# Multi-user mode

`main.py` serves one person from one config file. `service.py` serves many people
from a database, with each choosing up to three topics.

The single-user CLI is untouched and still works standalone. Multi-user is
additive: `store.py` + `catalog.yaml` + `service.py`.

```bash
python service.py demo --user wx_test   # onboard a fake user end to end
python service.py picker                # the topic list, as JSON for a client
python service.py build                 # today's batch, writes per-user HTML
python service.py stats                 # subscribers per topic, cost estimate
```

---

## The cost property

This is the design constraint everything else follows from.

A topic is fetched and summarized **once per day for the entire platform**. Users
subscribe to topic keys, not to jobs. Ten thousand people sharing three presets
is three summarization calls.

```
topics_in_use()  ->  fetch + summarize each ONCE  ->  renders table
                                                          |
        each user pulls their <=3 rendered topics and gets their own HTML
```

Measured on the demo: 4 users x 3 topics = 12 subscriptions, 6 LLM calls, because
all four picked `ai`.

| | preset-only | with custom topics |
|---|---|---|
| 1,000 users, ~40 distinct topics | ~$0.10/day | grows with *distinct* custom topics |
| 10,000 users, ~40 distinct topics | ~$0.10/day | same |

Custom topics are content-addressed: two users who describe the same thing get
the same `custom:<hash>` key and share one job. Cost tracks distinct *requests*,
not users — which is why the preset catalog should cover the common cases well
enough that most people never write one.

## Topics

**Presets** live in `catalog.yaml`. Each has a stable `key` — subscriptions
reference it, so never rename or reuse one. Deleting a preset that people still
subscribe to is handled: `resolve()` returns `None`, the batch logs a warning and
skips it, and those users still get their other topics.

**Custom topics** come from a free-text description, run through the same planner
the CLI wizard uses:

```python
t = service.create_custom_topic(store, "苹果和中国船舶的股价走势", language)
# {'key': 'custom:a1b2…', 'label': '股票', 'focus': 'Price moves and earnings…'}
service.choose_topics(store, catalog, openid, ["ai", t["key"]])
```

## The two invariants

**At most 3 topics.** `set_topics` replaces the entire selection atomically.
There is no append path, so no sequence of calls accumulates past the limit,
and re-submitting the same list is a no-op. Over-limit raises `ValueError` and
leaves the previous selection intact.

**Returning users keep their choices.** `open_session` touches `last_seen` and
nothing else. The only thing that changes a selection is an explicit
`set_topics`. Deactivating and reactivating an account preserves it too.

```python
s = service.open_session(store, openid)
if s["new_user"]:
    ...   # show the picker
else:
    ...   # s["topics"] is exactly what they saved
```

## Schema

| table | holds |
|---|---|
| `users` | openid, language, `onboarded`, `active` |
| `subscriptions` | (openid, topic_key, position) — at most 3 per user |
| `custom_topics` | global, content-addressed, shared between users |
| `renders` | (topic_key, day) -> items. The unit everyone shares. |
| `deliveries` | (openid, day) -> status. Makes retries safe. |

SQLite because it's one file with no ops. Every query is indexed and none join
across users, so this holds tens of thousands of accounts. When it stops being
enough, only `store.py` changes.

`renders` self-purges after 14 days. `prune_custom_topics()` drops customs nobody
subscribes to.

## Retry safety

`run_daily` is idempotent. Topics already built today come from cache
(`--force` to rebuild), and users already marked `sent` are skipped. A crashed
batch can be re-run without double-sending or double-paying.

---

## Before you build the WeChat client

Two platform constraints will shape the product more than any code here. Verify
both against current official docs before committing to a direction — these rules
change, and getting them wrong costs weeks.

**A mini program probably can't push a daily digest.** Long-term subscription
messages (长期订阅消息) are open only to specific second-level categories —
government services, medical, transport, finance, education. A daily reading
digest isn't one of them. What's left is one-time subscriptions (一次性订阅):
one authorization, one push. The common workaround is prompting the user to
authorize several at once, which buys a few days before they must return and
re-authorize.

**An official account can push daily, but only broadcasts.** A verified
subscription account (认证订阅号) can 群发 once per day — good cadence, wrong
shape: everyone receives identical content, which is the opposite of per-user
topics. Unverified personal subscription accounts don't get the broadcast API at
all. Service accounts (服务号) are limited to 4 broadcasts per month.

The architecture that resolves this: **official account for the daily nudge,
mini program for the personalized read.** Broadcast one short generic message
("今天的 digest 已更新") with a link into the mini program, where each user sees
their own three topics. Push cadence comes from the account; personalization
comes from the mini program. Neither platform limit gets in the way.

Two more things worth checking early, because they gate registration rather than
implementation:

- **Category.** Individual-entity mini programs are restricted to a narrow set of
  categories. News and information categories generally require an enterprise
  entity plus licensing (互联网新闻信息服务许可证), and finance content has its own
  qualification requirements — which is worth knowing before you ship the `a_shares`
  or `crypto` presets.
- **Content moderation.** Anything user-generated that gets displayed must pass
  `security.msgSecCheck`. Custom topic labels and descriptions are user-generated.
  Summaries derived from fetched news are worth checking too.

Sources: [长期订阅消息类目限制](https://developers.weixin.qq.com/community/develop/article/doc/00086293004d006da5ee7518c56813) ·
[订阅号群发接口规则](https://developers.weixin.qq.com/doc/subscription/guide/product/message/Batch_Sends.html) ·
[认证订阅号群发调整](https://cloud.tencent.com/developer/article/2264699) ·
[个人主体小程序类目](https://developers.weixin.qq.com/community/develop/doc/00026c36e34b98fabdc4f994f6b000)
