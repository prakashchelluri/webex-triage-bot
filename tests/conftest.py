from pathlib import Path

import pytest

from triage_bot.engine import Engine, load_rules
from triage_bot.store import Store

RULES = Path(__file__).resolve().parent.parent / "rules" / "example-rules.yaml"


@pytest.fixture
def engine():
    return Engine(load_rules(RULES), Store(":memory:"))
