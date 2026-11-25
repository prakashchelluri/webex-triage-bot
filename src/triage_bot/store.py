"""SQLite store for issued suggestions and per-rule feedback counts."""

from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS suggestions (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id TEXT NOT NULL,
    issue   TEXT NOT NULL,
    verdict TEXT,
    created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS rule_stats (
    rule_id TEXT PRIMARY KEY,
    correct INTEGER NOT NULL DEFAULT 0,
    wrong   INTEGER NOT NULL DEFAULT 0
);
"""

VERDICTS = ("correct", "wrong")


class Store:
    def __init__(self, path: str = ":memory:") -> None:
        # The webhook server handles one request at a time, but not always on the
        # thread that opened the store, so allow cross-thread use.
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(SCHEMA)

    def counts(self, rule_id: str) -> tuple[int, int]:
        row = self.db.execute(
            "SELECT correct, wrong FROM rule_stats WHERE rule_id = ?", (rule_id,)
        ).fetchone()
        return row or (0, 0)

    def factor(self, rule_id: str) -> float:
        """Learned multiplier: 2 * (correct + 1) / (correct + wrong + 2).

        Starts at 1.0 with no feedback, tends towards 2.0 for rules that are
        always confirmed and towards 0.0 for rules that are always wrong.
        """
        correct, wrong = self.counts(rule_id)
        return 2 * (correct + 1) / (correct + wrong + 2)

    def record_suggestion(self, rule_id: str, issue: str) -> int:
        with self.db:
            cur = self.db.execute(
                "INSERT INTO suggestions (rule_id, issue) VALUES (?, ?)", (rule_id, issue)
            )
        return cur.lastrowid

    def feedback(self, suggestion_id: int, verdict: str) -> str:
        """Apply feedback once per suggestion. Returns the rule id it counted against."""
        if verdict not in VERDICTS:
            raise ValueError(f"verdict must be one of {VERDICTS}")
        row = self.db.execute(
            "SELECT rule_id, verdict FROM suggestions WHERE id = ?", (suggestion_id,)
        ).fetchone()
        if row is None:
            raise LookupError(f"unknown suggestion #{suggestion_id}")
        rule_id, previous = row
        if previous is not None:
            raise ValueError(f"suggestion #{suggestion_id} already marked {previous}")
        with self.db:
            self.db.execute(
                "UPDATE suggestions SET verdict = ? WHERE id = ?", (verdict, suggestion_id)
            )
            self.db.execute(
                "INSERT OR IGNORE INTO rule_stats (rule_id) VALUES (?)", (rule_id,)
            )
            # verdict is validated above, so the column name is safe to interpolate
            self.db.execute(
                f"UPDATE rule_stats SET {verdict} = {verdict} + 1 WHERE rule_id = ?", (rule_id,)
            )
        return rule_id
