import json

import pytest
from conftest import OTHER_PARENT, PARENT, account

from bellhaven_sync.apply import StaleProposal, apply_proposal
from bellhaven_sync.proposals import NEW_ID, Proposal
from bellhaven_sync.store import Store


def proposal(fp="fp1", kind="update_fields", account_id="A1", actions=None, before=None):
    return Proposal(kind=kind, title="t", summary="s", location_slug="x", account_id=account_id, confidence="high",
                    actions=actions or [{"op": "update", "account_id": account_id, "fields": {"name": "New"}}],
                    before=before if before is not None else {"name": "Old"}, evidence={}, fingerprint=fp)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "state.db")


def test_rerun_does_not_duplicate_or_reask_decided(store):
    r1 = store.start_run()
    assert store.sync(r1, [proposal("a"), proposal("b")])["new"] == 2
    store.decide(store.list(("pending",))[0]["id"], "rejected")
    r2 = store.start_run()
    stats = store.sync(r2, [proposal("a"), proposal("b")])
    assert stats == {"new": 0, "still_pending": 1, "already_decided": 1, "obsolete": 0}
    assert len(store.list()) == 2


def test_pending_proposal_not_produced_again_goes_obsolete(store):
    store.sync(store.start_run(), [proposal("a")])
    stats = store.sync(store.start_run(), [])
    assert stats["obsolete"] == 1 and store.list()[0]["status"] == "obsolete"


class FakeCRM:
    def __init__(self, accounts):
        self.accounts = {a["account_id"]: dict(a) for a in accounts}
        self.calls, self.on_write, self.created = [], None, 0

    def get_account(self, aid):
        return dict(self.accounts[aid])

    def list_accounts(self, **filters):
        return [a for a in self.accounts.values() if all(a.get("billing_zip") == v for k, v in filters.items())]

    def update_account(self, aid, fields):
        self.calls.append(("PATCH", aid, fields))
        self.accounts[aid].update(fields)
        return self.accounts[aid]

    def create_account(self, fields):
        self.created += 1
        aid = f"NEW{self.created}"
        self.accounts[aid] = {**fields, "account_id": aid, "created_by_candidate": True}
        self.calls.append(("POST", aid, fields))
        return self.accounts[aid]


def test_apply_stops_if_crm_changed_since_the_run(store):
    store.sync(store.start_run(), [proposal("a", before={"name": "Old"})])
    pid = store.list()[0]["id"]
    crm = FakeCRM([account("A1", "Someone renamed it", "1 Main St", "X", "OH", "11111")])
    with pytest.raises(StaleProposal):
        apply_proposal(store, pid, crm)
    assert crm.calls == []


def test_chow_creates_then_links_and_never_creates_twice(store):
    old = account("A6", "Bellhaven of Tiffin", "45 St Lawrence Dr", "Tiffin", "OH", "44883", parent=OTHER_PARENT,
                  lifetime_revenue=84000, outstanding_ar=12400)
    new_fields = {"name": "Bellhaven of Tiffin", "parent_id": PARENT["account_id"], "billing_zip": "44883"}
    p = proposal("chow", kind="chow", account_id="A6",
                 actions=[{"op": "create", "fields": new_fields},
                          {"op": "update", "account_id": "A6", "fields": {"chow_current_account": NEW_ID}}],
                 before={"chow_current_account": "", "parent_id": OTHER_PARENT["account_id"]})
    store.sync(store.start_run(), [p])
    pid = store.list()[0]["id"]
    crm = FakeCRM([old])
    result = apply_proposal(store, pid, crm)
    assert crm.accounts["A6"]["chow_current_account"] == result["created_account_id"] == "NEW1"
    assert crm.accounts["A6"]["parent_id"] == OTHER_PARENT["account_id"]  # old account left as it was
    # A retry after a crash between the two calls reuses the account it already created.
    crm.accounts["A6"]["chow_current_account"] = ""
    apply_proposal(store, pid, crm)
    assert crm.created == 1


def test_sop_rechecked_at_apply_time(store):
    acc = account("A5", "Cedar Trail of Zanesville", "2680 Maple Ave", "Zanesville", "OH", "43701", parent=OTHER_PARENT)
    p = proposal("rp", kind="reparent", account_id="A5",
                 actions=[{"op": "update", "account_id": "A5", "fields": {"parent_id": PARENT["account_id"]}}],
                 before={"parent_id": OTHER_PARENT["account_id"]})
    store.sync(store.start_run(), [p])
    crm = FakeCRM([{**acc, "lifetime_revenue": 1000, "outstanding_ar": 50}])  # billing appeared since the run
    with pytest.raises(StaleProposal):
        apply_proposal(store, store.list()[0]["id"], crm)
    assert crm.calls == []
