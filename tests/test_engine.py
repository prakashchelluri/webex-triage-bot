import pytest

from triage_bot.engine import Engine, format_markdown, load_rules
from triage_bot.store import Store


def write_rules(tmp_path, body):
    p = tmp_path / "rules.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def test_example_rules_load(engine):
    ids = {r.id for r in engine.rules}
    assert {"crashloop-backoff", "image-pull-backoff", "etcd-slow", "node-disk-pressure",
            "oom-killed", "certificate-expiry", "pvc-pending"} <= ids


@pytest.mark.parametrize("text,rule_id", [
    ("payments pod in CrashLoopBackOff after deploy", "crashloop-backoff"),
    ("Failed to pull image: ImagePullBackOff, manifest unknown", "image-pull-backoff"),
    ("etcdserver: request timed out on dev cluster", "etcd-slow"),
    ("node worker-3 has DiskPressure, pods evicted", "node-disk-pressure"),
    ("container OOMKilled with exit code 137", "oom-killed"),
    ("x509: certificate has expired or is not yet valid", "certificate-expiry"),
    ("PVC data-db-0 stuck in Pending", "pvc-pending"),
])
def test_top_suggestion(engine, text, rule_id):
    assert engine.suggest(text).suggestions[0].rule.id == rule_id


def test_keyword_is_whole_word_and_case_insensitive(engine):
    rule = next(r for r in engine.rules if r.id == "oom-killed")
    assert rule.match("pod was oomkilled") == ["keyword `OOMKilled`"]
    assert rule.match("notOOMKilledreally") == []


def test_more_matches_rank_higher_and_confidence(engine):
    res = engine.suggest("OOMKilled exit code 137, out of memory; also x509 noise")
    assert [s.rule.id for s in res.suggestions] == ["oom-killed", "certificate-expiry"]
    assert res.suggestions[0].confidence == pytest.approx(0.875)
    assert res.suggestions[1].confidence == pytest.approx(0.5)


def test_no_match_returns_empty(engine):
    res = engine.suggest("the coffee machine is broken")
    assert res.suggestions == [] and res.fallback is None
    assert "No team rule matched" in format_markdown(res)


def test_feedback_reorders_rules(tmp_path):
    rules = load_rules(write_rules(tmp_path, """
rules:
  - {id: a, keywords: [timeout], fix: fix a}
  - {id: b, keywords: [timeout], fix: fix b}
"""))
    engine = Engine(rules, Store(":memory:"))
    first = engine.suggest("timeout").suggestions
    assert first[0].rule.id == "a"  # tie keeps file order
    engine.feedback(first[0].id, "wrong")
    engine.feedback(first[1].id, "correct")
    again = engine.suggest("timeout").suggestions
    assert [s.rule.id for s in again] == ["b", "a"]
    assert again[0].factor == pytest.approx(4 / 3)
    assert again[1].factor == pytest.approx(2 / 3)


def test_feedback_only_once_and_unknown(engine):
    sid = engine.suggest("CrashLoopBackOff").suggestions[0].id
    engine.feedback(sid, "correct")
    with pytest.raises(ValueError, match="already marked"):
        engine.feedback(sid, "wrong")
    with pytest.raises(LookupError):
        engine.feedback(9999, "correct")


def test_learned_weight_persists(tmp_path):
    db = str(tmp_path / "t.db")
    rules = load_rules(write_rules(tmp_path, "rules:\n  - {id: a, keywords: [x], fix: f}\n"))
    e1 = Engine(rules, Store(db))
    e1.feedback(e1.suggest("x").suggestions[0].id, "correct")
    e1.store.db.close()
    assert Store(db).factor("a") == pytest.approx(4 / 3)


class FakeProvider:
    def __init__(self):
        self.calls = []

    def suggest(self, text):
        self.calls.append(text)
        return "try turning it off and on"


def test_llm_fallback_only_below_threshold(engine):
    engine.provider = FakeProvider()
    res = engine.suggest("the coffee machine is broken")
    assert res.fallback == "try turning it off and on"
    assert "Model suggestion (unverified)" in format_markdown(res)
    engine.suggest("CrashLoopBackOff, back-off restarting failed container")
    assert len(engine.provider.calls) == 1


@pytest.mark.parametrize("body,msg", [
    ("rules:\n  - {id: a, fix: f}\n", "at least one keyword"),
    ("rules:\n  - {id: a, keywords: [x]}\n", "'fix' is required"),
    ("rules:\n  - {id: a, patterns: ['('], fix: f}\n", "bad regex"),
    ("rules:\n  - {id: a, keywords: [x], fix: f}\n  - {id: a, keywords: [y], fix: f}\n",
     "duplicated"),
])
def test_invalid_rules(tmp_path, body, msg):
    with pytest.raises(ValueError, match=msg):
        load_rules(write_rules(tmp_path, body))


def test_markdown_format(engine):
    md = format_markdown(engine.suggest("pod OOMKilled"))
    assert md.startswith("**Triage suggestions**")
    assert "**#1 `oom-killed`** (workload), confidence 0.50, learned x1.00" in md
    assert "Runbook: https://runbooks.example.com/k8s/oomkilled" in md
    assert "Matched: keyword `OOMKilled`" in md
    assert "feedback <id> correct" in md
