"""Apply an approved proposal to the CRM. This is the only module that writes.

Safety checks before any write:
  1. Re-read the account. If any field we're about to change no longer has the value the proposal was
     based on, stop: someone changed the CRM since the run, so the proposal may be wrong now.
  2. For parent changes, re-run the SOP on the fresh revenue/AR numbers. If the branch changed
     (e.g. AR was paid off), stop and let the next run propose the right thing.
  3. Creates are never retried blindly. The new account id is saved the moment it exists, and a retry
     looks for an account we already created before making another one.
"""
from __future__ import annotations

import json

from .crm import CRMClient
from .proposals import NEW_ID, sop_branch
from .store import Store


class StaleProposal(RuntimeError):
    pass


def _same(a, b) -> bool:
    return str(a if a is not None else "") == str(b if b is not None else "")


def apply_proposal(store: Store, proposal_id: int, client: CRMClient | None = None) -> dict:
    row = store.get(proposal_id)
    if row is None:
        raise KeyError(proposal_id)
    payload = json.loads(row["payload_json"])
    result = json.loads(row["result_json"] or "{}")
    client = client or CRMClient()
    client.on_write = lambda entry: store.log_write(proposal_id, entry)

    if row["account_id"] and payload["before"]:
        fresh = client.get_account(row["account_id"])
        drift = {k: {"expected": v, "now": fresh.get(k)} for k, v in payload["before"].items() if not _same(fresh.get(k), v)}
        if drift:
            raise StaleProposal(f"The CRM changed since this proposal was made: {drift}. Re-run the pipeline.")
        if row["kind"] in ("reparent", "chow") and sop_branch(fresh) != row["kind"]:
            raise StaleProposal("Revenue or AR changed since this proposal was made, so the SOP branch is different now. "
                                "Re-run the pipeline.")

    new_id = result.get("created_account_id")
    for action in payload["actions"]:
        if action["op"] == "create":
            if new_id:
                continue
            fields = action["fields"]
            existing = [a for a in client.list_accounts(zip=fields["billing_zip"])
                        if a.get("created_by_candidate") and a["name"] == fields["name"] and a.get("parent_id") == fields["parent_id"]]
            new_id = existing[0]["account_id"] if existing else client.create_account(fields)["account_id"]
            result["created_account_id"] = new_id
            store.save_result(proposal_id, result)  # persist now, so a failure later can't cause a second create
        elif action["op"] == "update":
            fields = {k: (new_id if v == NEW_ID else v) for k, v in action["fields"].items()}
            client.update_account(action["account_id"], fields)
            result.setdefault("updated", []).append(action["account_id"])
    result["done"] = True
    return result
