"""Optional LLM fallback, used only when no rule matches with enough confidence.

A provider is any class with a ``suggest(issue_text) -> str | None`` method and a
no-argument constructor. Select one with ``TRIAGE_LLM_PROVIDER=package.module:ClassName``.
The default, ``none``, makes no network calls and never suggests anything.
"""

from __future__ import annotations

import importlib
from typing import Protocol


class Provider(Protocol):
    def suggest(self, issue_text: str) -> str | None: ...


class NoneProvider:
    def suggest(self, issue_text: str) -> str | None:
        return None


def load_provider(spec: str) -> Provider:
    if spec in ("", "none"):
        return NoneProvider()
    module, _, name = spec.partition(":")
    if not name:
        raise ValueError(f"provider must look like 'package.module:ClassName', got {spec!r}")
    return getattr(importlib.import_module(module), name)()
