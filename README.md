# webex-triage-bot

A Webex chat bot that suggests fixes for incoming SRE issues from rules your team writes, and learns from your feedback which rules to trust.

> Lab project: a clean-room reference implementation of a pattern I use at work. It contains no employer code or data.

## The problem

An SRE team that owns Kubernetes/OpenShift platforms gets the same classes of issue assigned again and again: CrashLoopBackOff, ImagePullBackOff, OOMKilled pods, nodes under DiskPressure, slow etcd, expiring certificates, PVCs stuck in Pending. The first-response steps are well known to the senior people on the team, but they live in heads and scattered runbooks, so newer engineers (and tired on-call engineers) lose time rediscovering them.

This bot keeps those first-response steps as plain, reviewable rules in YAML. When someone posts an issue in a Webex room, the bot replies with the best-matching proposed fixes, why they matched, and a runbook link. Engineers reply `feedback <id> correct` or `feedback <id> wrong`, and rules that keep being wrong sink in the ranking while confirmed ones rise.

## Quick start

Requires Python 3.11+.

```bash
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest -q

triage-bot suggest "pod api-7f9c OOMKilled, exit code 137"
triage-bot feedback 1 correct
```

By default the CLI reads `rules/example-rules.yaml` and stores feedback in `triage.db` in the current directory. Override with `--rules` / `--db` or `TRIAGE_RULES` / `TRIAGE_DB`.

### Running it as a Webex bot

1. Create a bot at developer.webex.com and copy its access token.
2. Expose the server over HTTPS (reverse proxy or tunnel) and register a webhook for `resource=messages`, `event=created` pointing at it, with a secret.
3. Run:

```bash
export WEBEX_BOT_TOKEN=...        # bot access token
export WEBHOOK_SECRET=...         # same secret as the webhook registration
triage-bot serve --host 0.0.0.0 --port 8080
```

In a group room, mention the bot with the issue text. In a 1:1 room, just paste it. `help` prints usage.

## Example output

Real output from a local run against the example rules (fresh database):

```text
$ triage-bot suggest "etcdserver: request timed out, apply request took too long on dev cluster; api server slow"
**Triage suggestions**
1. **#1 `etcd-slow`** (control-plane), confidence 0.92, learned x1.00
   - Fix: etcd is latency-sensitive to disk. Check wal_fsync and backend_commit duration histograms (p99 should stay under ~10ms / ~25ms), confirm etcd is on SSD with enough IOPS, and look for noisy neighbours writing to the same disk. Defragment if the DB size is close to its quota.
   - Runbook: https://runbooks.example.com/k8s/etcd-latency
   - Matched: keyword `etcdserver`, regex `etcd.*(timed? ?out|timeout|slow)`, regex `apply request took too long`
Reply `feedback <id> correct` or `feedback <id> wrong` so I can learn.

$ triage-bot suggest "pod api-7f9c OOMKilled, exit code 137"
**Triage suggestions**
1. **#2 `oom-killed`** (workload), confidence 0.75, learned x1.00
   - Fix: The container exceeded its memory limit. Compare actual usage (container_memory_working_set_bytes) with the limit; raise the limit if usage is legitimate, otherwise look for a leak. For JVMs, align -Xmx / MaxRAMPercentage with the container limit.
   - Runbook: https://runbooks.example.com/k8s/oomkilled
   - Matched: keyword `OOMKilled`, regex `exit code:? 137`
Reply `feedback <id> correct` or `feedback <id> wrong` so I can learn.

$ triage-bot feedback 2 wrong
Thanks. Marked suggestion #2 as wrong; rule `oom-killed` learned weight is now x0.67.

$ triage-bot feedback 2 correct
error: suggestion #2 already marked wrong

$ triage-bot suggest "the printer is on fire"
No team rule matched this issue.

$ triage-bot feedback 1 correct
Thanks. Marked suggestion #1 as correct; rule `etcd-slow` learned weight is now x1.33.

$ triage-bot suggest "etcd leader changed, request timed out"
**Triage suggestions**
1. **#3 `etcd-slow`** (control-plane), confidence 0.89, learned x1.33
   ...
   - Matched: regex `etcd.*(timed? ?out|timeout|slow)`, regex `leader changed`
```

The Webex reply is the same markdown, posted to the room.

## How it works

**Rules** (`rules/example-rules.yaml`): each rule has an `id`, `category`, `keywords` (whole-word, case-insensitive) and/or `patterns` (regex, case-insensitive), a proposed `fix`, an optional `runbook` URL and an optional base `weight` (default 1.0). The loader rejects rules with no matchers, no fix, a broken regex or a duplicate id.

**Scoring**: for every rule,

```
score      = weight * (number of keywords/patterns that matched) * learned_factor
confidence = 1 - 0.5 ** score
```

so one plain match gives 0.50, two give 0.75, three give 0.875. The top 3 rules by score are returned, and each gets a suggestion id saved in SQLite so feedback can refer to it.

**Learning from mistakes**: each rule keeps a count of `correct` and `wrong` verdicts. Its learned factor is

```
learned_factor = 2 * (correct + 1) / (correct + wrong + 2)
```

That is twice the rule's smoothed hit rate: 1.0 with no feedback, 1.33 after one confirmation, 0.67 after one rejection, approaching 2.0 for rules that are always right and 0 for rules that are always wrong. The +1/+2 smoothing stops one early verdict from dominating. Each suggestion accepts feedback once.

**LLM fallback (optional, off by default)**: when no rule matches, or the best confidence is below `--threshold` (default 0.5), the engine asks a provider. The default provider, `none`, returns nothing and makes no network calls; no vendor SDK is a dependency. To plug one in, write a class with a no-argument constructor and a `suggest(self, issue_text: str) -> str | None` method, make it importable, and select it:

```bash
export TRIAGE_LLM_PROVIDER=my_team.llm:InternalModelProvider
```

Its answer is shown separately as "Model suggestion (unverified)" so it is never confused with a team rule.

**Webex**: `triage-bot serve` runs a small `http.server` webhook endpoint. For each POST it verifies `X-Spark-Signature` (HMAC-SHA1 of the raw body with `WEBHOOK_SECRET`, constant-time compare) when a secret is set, ignores non-message events and the bot's own messages, fetches the message text from `GET /v1/messages/{id}` (webhooks do not carry the text), and posts the reply with `POST /v1/messages` using `markdown`. All HTTP uses `urllib` with `WEBEX_BOT_TOKEN`.

```
src/triage_bot/
  engine.py   rule loading, scoring, ranking, markdown formatting
  store.py    SQLite: issued suggestions and per-rule feedback counts
  llm.py      provider interface, "none" provider, provider loader
  webex.py    signature check, Webex REST client, webhook server
  cli.py      triage-bot suggest | feedback | serve
```

## Limitations

- Matching is keyword/regex only; there is no stemming, synonyms or semantic similarity. Rule quality is everything.
- Learning is per rule, not per rule-and-context: a rule that is right for one kind of issue and wrong for another just averages out.
- Feedback is not tied to who gave it; anyone in the room can mark a suggestion.
- The webhook server handles one request at a time and replies synchronously. That is fine for a team room, not for high volume. Run it behind a TLS-terminating proxy; it does not serve HTTPS itself.
- No retries or rate-limit handling for the Webex API; a failed call is logged and the webhook gets a 500.
- Attachments, adaptive cards and threads are not handled; only message text.
- Not tested against the live Webex API in this repository; tests mock all HTTP.

## License

MIT
