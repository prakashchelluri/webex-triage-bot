"""Rule loading, matching, ranking and reply formatting."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .llm import NoneProvider, Provider
from .store import Store


@dataclass(frozen=True)
class Rule:
    id: str
    category: str
    fix: str
    matchers: tuple[tuple[str, re.Pattern[str]], ...]
    runbook: str | None = None
    weight: float = 1.0

    def match(self, text: str) -> list[str]:
        """Return the labels of every keyword/pattern that matches the text."""
        return [label for label, rx in self.matchers if rx.search(text)]


@dataclass
class Suggestion:
    id: int
    rule: Rule
    matched: list[str]
    factor: float
    score: float

    @property
    def confidence(self) -> float:
        # Each matched pattern halves the remaining doubt: 1 hit -> 0.50, 2 -> 0.75, 3 -> 0.875.
        return 1 - 0.5**self.score


@dataclass
class Result:
    suggestions: list[Suggestion] = field(default_factory=list)
    fallback: str | None = None


def load_rules(path: str | Path) -> list[Rule]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    rules: list[Rule] = []
    seen: set[str] = set()
    for raw in data.get("rules", []):
        rid = raw.get("id")
        if not rid or rid in seen:
            raise ValueError(f"rule id missing or duplicated: {rid!r}")
        seen.add(rid)
        if not raw.get("fix"):
            raise ValueError(f"rule {rid}: 'fix' is required")
        matchers = [
            (f"keyword `{k}`", re.compile(rf"\b{re.escape(k)}\b", re.IGNORECASE))
            for k in raw.get("keywords", [])
        ]
        try:
            matchers += [
                (f"regex `{p}`", re.compile(p, re.IGNORECASE)) for p in raw.get("patterns", [])
            ]
        except re.error as exc:
            raise ValueError(f"rule {rid}: bad regex: {exc}") from exc
        if not matchers:
            raise ValueError(f"rule {rid}: needs at least one keyword or pattern")
        rules.append(
            Rule(
                id=rid,
                category=raw.get("category", "general"),
                fix=raw["fix"].strip(),
                matchers=tuple(matchers),
                runbook=raw.get("runbook"),
                weight=float(raw.get("weight", 1.0)),
            )
        )
    return rules


class Engine:
    def __init__(
        self,
        rules: list[Rule],
        store: Store,
        provider: Provider | None = None,
        threshold: float = 0.5,
        top: int = 3,
    ) -> None:
        self.rules = rules
        self.store = store
        self.provider = provider or NoneProvider()
        self.threshold = threshold
        self.top = top

    def suggest(self, text: str) -> Result:
        ranked = []
        for rule in self.rules:
            matched = rule.match(text)
            if matched:
                factor = self.store.factor(rule.id)
                ranked.append((rule.weight * len(matched) * factor, rule, matched, factor))
        ranked.sort(key=lambda r: r[0], reverse=True)

        result = Result()
        for score, rule, matched, factor in ranked[: self.top]:
            sid = self.store.record_suggestion(rule.id, text)
            result.suggestions.append(Suggestion(sid, rule, matched, factor, score))

        if not result.suggestions or result.suggestions[0].confidence < self.threshold:
            result.fallback = self.provider.suggest(text)
        return result

    def feedback(self, suggestion_id: int, verdict: str) -> str:
        rule_id = self.store.feedback(suggestion_id, verdict)
        return (
            f"Thanks. Marked suggestion #{suggestion_id} as {verdict}; "
            f"rule `{rule_id}` learned weight is now x{self.store.factor(rule_id):.2f}."
        )


def format_markdown(result: Result) -> str:
    lines = []
    if result.suggestions:
        lines.append("**Triage suggestions**")
        for n, s in enumerate(result.suggestions, 1):
            lines.append(
                f"{n}. **#{s.id} `{s.rule.id}`** ({s.rule.category}), "
                f"confidence {s.confidence:.2f}, learned x{s.factor:.2f}"
            )
            lines.append(f"   - Fix: {s.rule.fix}")
            if s.rule.runbook:
                lines.append(f"   - Runbook: {s.rule.runbook}")
            lines.append(f"   - Matched: {', '.join(s.matched)}")
    else:
        lines.append("No team rule matched this issue.")
    if result.fallback:
        lines.append(f"**Model suggestion (unverified):** {result.fallback}")
    if result.suggestions:
        lines.append("Reply `feedback <id> correct` or `feedback <id> wrong` so I can learn.")
    return "\n".join(lines)
