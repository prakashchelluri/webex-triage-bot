"""Command line entry point: triage-bot suggest | feedback | serve."""

from __future__ import annotations

import argparse
import logging
import os
import sys

from .engine import Engine, format_markdown, load_rules
from .llm import load_provider
from .store import Store


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="triage-bot", description=__doc__)
    p.add_argument(
        "--rules", default=os.environ.get("TRIAGE_RULES", "rules/example-rules.yaml")
    )
    p.add_argument("--db", default=os.environ.get("TRIAGE_DB", "triage.db"))
    p.add_argument(
        "--threshold", type=float, default=0.5,
        help="LLM fallback is asked when the best confidence is below this (default 0.5)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("suggest", help="suggest fixes for an issue text")
    s.add_argument("text")
    f = sub.add_parser("feedback", help="mark a suggestion correct or wrong")
    f.add_argument("suggestion_id", type=int)
    f.add_argument("verdict", choices=["correct", "wrong"])
    v = sub.add_parser("serve", help="run the Webex webhook server")
    v.add_argument("--host", default="0.0.0.0")
    v.add_argument("--port", type=int, default=8080)
    args = p.parse_args(argv)

    engine = Engine(
        load_rules(args.rules),
        Store(args.db),
        provider=load_provider(os.environ.get("TRIAGE_LLM_PROVIDER", "none")),
        threshold=args.threshold,
    )

    if args.cmd == "suggest":
        print(format_markdown(engine.suggest(args.text)))
    elif args.cmd == "feedback":
        try:
            print(engine.feedback(args.suggestion_id, args.verdict))
        except (LookupError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    else:
        from .webex import WebexClient, serve

        token = os.environ.get("WEBEX_BOT_TOKEN")
        if not token:
            print("error: WEBEX_BOT_TOKEN is not set", file=sys.stderr)
            return 2
        logging.basicConfig(level=logging.INFO)
        serve(engine, WebexClient(token), os.environ.get("WEBHOOK_SECRET"), args.host, args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
