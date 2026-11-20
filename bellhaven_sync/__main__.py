"""Command line.

    python -m bellhaven_sync run            scrape, match and queue proposals (never writes to the CRM)
    python -m bellhaven_sync serve          open the review app at http://127.0.0.1:5000
    python -m bellhaven_sync status         queue counts and the last run's summary
    python -m bellhaven_sync scrape         print the scraped locations as JSON
"""
from __future__ import annotations

import argparse
import json
import logging
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bellhaven_sync", description="Sync Bellhaven facility ownership into the CRM.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="scrape, match and queue proposals for review")
    serve = sub.add_parser("serve", help="start the review app")
    serve.add_argument("--port", type=int, default=5000)
    serve.add_argument("--host", default="127.0.0.1")
    sub.add_parser("status", help="show queue counts and the last run")
    sub.add_parser("scrape", help="print scraped locations as JSON")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.cmd == "run":
        from .pipeline import run
        print(json.dumps(run(), indent=2))
    elif args.cmd == "serve":
        from .review_app import create_app
        create_app().run(host=args.host, port=args.port, debug=False)
    elif args.cmd == "status":
        from .store import Store
        store = Store()
        last = store.last_run()
        print(json.dumps({"queue": store.counts(), "last_run": json.loads(last["stats_json"]) if last else None}, indent=2))
    elif args.cmd == "scrape":
        from .scraper import scrape
        r = scrape()
        print(json.dumps({"claimed": r.claimed_count, "warnings": r.warnings,
                          "locations": [l.to_dict() for l in r.locations]}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
