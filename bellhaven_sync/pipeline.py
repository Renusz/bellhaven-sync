"""The daily pipeline: scrape -> read the CRM -> match -> propose -> queue for review. It never writes to the CRM."""
from __future__ import annotations

import json
import logging
from collections import Counter

from . import config, matching, proposals, scraper
from .crm import CRMClient
from .store import Store

log = logging.getLogger(__name__)


def run(store: Store | None = None, client: CRMClient | None = None, scrape=scraper.scrape) -> dict:
    store = store or Store()
    run_id = store.start_run()

    site = scrape()
    client = client or CRMClient()
    accounts, contacts = client.list_accounts(), client.list_contacts()

    result = matching.match(site.locations, accounts, contacts)
    props = proposals.build(result, site.pages_visited)
    queue = store.sync(run_id, props)
    classification = proposals.classify(result, props)

    stats = {
        "run_id": run_id,
        "website_locations": len(site.locations),
        "website_claims": site.claimed_count,
        "crm_accounts": len(accounts),
        "crm_contacts": len(contacts),
        "classification": dict(Counter(r["classification"] for r in classification)),
        "duplicate_copies": sum(len(m.duplicates) for m in result.matches),
        "proposals_this_run": dict(Counter(p.kind for p in props)),
        "queue": queue,
        "warnings": site.warnings,
    }
    store.finish_run(run_id, stats, classification)
    out = config.STATE_DB.parent / "last_run.json"
    out.write_text(json.dumps({"stats": stats, "classification": classification}, indent=2), encoding="utf-8")
    log.info("run %s: %s", run_id, json.dumps(stats))
    return stats
