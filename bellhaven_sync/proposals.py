"""Proposals: turn match results into concrete CRM changes, each with its evidence.

Proposals are computed as "desired state minus current state": if the CRM already looks right, nothing is
proposed. That is half of what makes re-runs safe. The other half is the decision store (store.py),
which remembers rejected proposals so they are never asked again.

Kinds, in the order a reviewer sees them:
    chow                 SOP: the account has revenue AND outstanding AR, so keep it as is, create a new
                         account under Bellhaven, and point the old one's chow_current_account at it
    reparent             move the account under Bellhaven (no revenue history, or no outstanding AR)
    mark_duplicate       another copy of the same facility: duplicate_of_account = survivor, status Inactive
    create_account       on the website but not in the CRM
    update_fields        outdated name, zip typo, care type, or an inactive account that is live again
    flag_not_on_website  under Bellhaven in the CRM, but no longer on the website: Needs Review + note
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field

from . import config
from .matching import Candidate, LocationMatch, MatchResult
from .normalize import same_name
from .scraper import Location

KIND_ORDER = ["chow", "reparent", "mark_duplicate", "create_account", "update_fields", "flag_not_on_website"]
KIND_LABEL = {"chow": "Change of ownership (SOP)", "reparent": "Re-parent", "mark_duplicate": "Duplicate",
              "create_account": "New account", "update_fields": "Fix fields", "flag_not_on_website": "Not on website"}
MARKER = "[bellhaven-sync]"
NEW_ID = "$new"  # placeholder for the id of the account created earlier in the same proposal

ACCOUNT_FIELDS = ["account_id", "name", "parent_id", "parent_name", "billing_street", "billing_city", "billing_state",
                  "billing_zip", "care_type", "status", "phone", "lifetime_revenue", "outstanding_ar",
                  "chow_current_account", "duplicate_of_account", "note", "created_by_candidate"]


@dataclass
class Proposal:
    kind: str
    title: str
    summary: str
    location_slug: str | None
    account_id: str | None
    confidence: str
    actions: list[dict]          # [{"op": "update"|"create", "account_id": ..., "fields": {...}}]
    before: dict                 # current values of every field we change; re-checked before writing
    evidence: dict = field(default_factory=dict)
    fingerprint: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def fingerprint(*parts) -> str:
    return hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


def sop_branch(account: dict) -> str:
    """The billing SOP. 'chow' only when there is revenue history AND outstanding AR greater than zero."""
    has_revenue = (account.get("lifetime_revenue") or 0) > 0
    has_ar = (account.get("outstanding_ar") or 0) > 0
    return "chow" if has_revenue and has_ar else "reparent"


def _snapshot(acc: dict) -> dict:
    return {k: acc.get(k) for k in ACCOUNT_FIELDS}


def _append_note(existing: str | None, text: str) -> str:
    existing = (existing or "").strip()
    return f"{existing}\n{text}" if existing else text


def _website_fields(loc: Location, parent_id: str) -> dict:
    return {"name": loc.name, "parent_id": parent_id, "billing_street": loc.street, "billing_city": loc.city,
            "billing_state": loc.state, "billing_zip": loc.zip, "care_type": (loc.care_types or [""])[0],
            "phone": loc.phone, "status": "Active"}


def _sop_evidence(acc: dict, branch: str) -> dict:
    rev, ar = acc.get("lifetime_revenue") or 0, acc.get("outstanding_ar") or 0
    if branch == "chow":
        why = (f"Lifetime revenue ${rev:,.0f} AND outstanding AR ${ar:,.0f} > 0: billing needs the old account preserved. "
               "Leave it exactly as it is, create a new account under the correct parent, and set chow_current_account "
               "on the old account to the new id.")
    elif rev > 0:
        why = f"Revenue history (${rev:,.0f}) but no outstanding AR, so the SOP says re-parent the existing account directly."
    elif ar > 0:
        why = f"Outstanding AR (${ar:,.0f}) but no revenue history, so the SOP says re-parent the existing account directly."
    else:
        why = "No revenue history and no outstanding AR, so the SOP says re-parent the existing account directly."
    return {"lifetime_revenue": rev, "outstanding_ar": ar, "branch": branch, "rule": why}


def _location_evidence(m: LocationMatch, cand: Candidate | None) -> dict:
    ev = {"website": m.location.to_dict(), "near_misses": [n.to_dict() for n in m.near_misses]}
    if cand:
        ev["crm"] = _snapshot(cand.account)
        ev["match"] = cand.to_dict()
    if len(m.accepted) > 1:
        ev["cluster"] = [{**c.to_dict(), "role": "survivor" if c is m.survivor else "duplicate"} for c in m.accepted]
        ev["survivor_reason"] = m.survivor_reason
    return ev


def _for_location(m: LocationMatch, parent: dict) -> list[Proposal]:
    loc, out = m.location, []
    pid, pname = parent["account_id"], parent["name"]

    if m.survivor is None:
        fields = _website_fields(loc, pid)
        fields["note"] = f"{MARKER} Created from the operator website: {loc.url}"
        lookalikes = ", ".join(f"'{n.account['name']}' ({n.disqualified or str(n.score) + ' pts'})" for n in m.near_misses)
        out.append(Proposal(
            kind="create_account", title=f"Create {loc.name}",
            summary=f"{loc.name} ({loc.city}, {loc.state}) is on the website but has no CRM account."
                    + (f" Lookalikes checked and rejected: {lookalikes}." if lookalikes else ""),
            location_slug=loc.slug, account_id=None, confidence="high",
            actions=[{"op": "create", "fields": fields}], before={},
            evidence={**_location_evidence(m, None), "changes": [{"field": k, "before": None, "after": v} for k, v in fields.items()]},
            fingerprint=fingerprint("create_account", loc.slug, loc.street, loc.zip)))
        return out

    s = m.survivor
    acc = s.account
    ev = _location_evidence(m, s)

    # Parent: the SOP decides between moving the account and a change of ownership.
    branch = None
    if acc.get("parent_id") != pid:
        branch = sop_branch(acc)
        old_parent = acc.get("parent_name") or "no parent"
        sop = _sop_evidence(acc, branch)
        if branch == "chow":
            new_fields = _website_fields(loc, pid)
            new_fields["note"] = f"{MARKER} Change of ownership: replaces {acc['account_id']} ({acc['name']}, under {old_parent}), kept for billing."
            out.append(Proposal(
                kind="chow", title=f"Change of ownership: {loc.name}",
                summary=f"{loc.name} is on Bellhaven's website but its account sits under {old_parent}. It has revenue AND "
                        f"outstanding AR, so per the SOP the old account stays untouched and a new one is created under {pname}.",
                location_slug=loc.slug, account_id=acc["account_id"], confidence=s.confidence,
                actions=[{"op": "create", "fields": new_fields},
                         {"op": "update", "account_id": acc["account_id"], "fields": {"chow_current_account": NEW_ID}}],
                before={"chow_current_account": acc.get("chow_current_account") or "", "parent_id": acc.get("parent_id") or ""},
                evidence={**ev, "sop": sop, "changes": [
                    {"field": "new account", "before": None, "after": f"{loc.name} under {pname}"},
                    {"field": "chow_current_account (old account)", "before": acc.get("chow_current_account") or "", "after": "id of the new account"}]},
                fingerprint=fingerprint("chow", acc["account_id"], pid)))
        else:
            out.append(Proposal(
                kind="reparent", title=f"Move {acc['name']} under Bellhaven",
                summary=f"{loc.name} is on Bellhaven's website but its account is under {old_parent}. {sop['rule']}",
                location_slug=loc.slug, account_id=acc["account_id"], confidence=s.confidence,
                actions=[{"op": "update", "account_id": acc["account_id"], "fields": {"parent_id": pid}}],
                before={"parent_id": acc.get("parent_id") or ""},
                evidence={**ev, "sop": sop, "changes": [{"field": "parent", "before": old_parent, "after": pname}]},
                fingerprint=fingerprint("reparent", acc["account_id"], pid)))

    # Field fixes on the surviving account (never on an account the SOP says to leave exactly as it is).
    if branch != "chow":
        changes, fields = [], {}
        if acc.get("name", "").strip() != loc.name:
            why = ("same name, different spelling; aligned to the website" if same_name(acc.get("name"), loc.name)
                   else "outdated name (rebrand); the website is the operator's current name")
            changes.append({"field": "name", "before": acc.get("name"), "after": loc.name, "reason": why})
        if s.zip_differs:
            changes.append({"field": "billing_zip", "before": acc.get("billing_zip"), "after": loc.zip,
                            "reason": "same street and city; the CRM zip looks like a typo"})
        if loc.care_types and acc.get("care_type") not in loc.care_types:
            changes.append({"field": "care_type", "before": acc.get("care_type"), "after": loc.care_types[0],
                            "reason": f"website lists {', '.join(loc.care_offerings)}"})
        if acc.get("status") == "Inactive":
            changes.append({"field": "status", "before": "Inactive", "after": "Active",
                            "reason": "the facility is listed on the website"})
        for c in changes:
            fields[c["field"]] = c["after"]
        if fields:
            what = ", ".join(c["field"].replace("billing_", "") for c in changes)
            out.append(Proposal(
                kind="update_fields", title=f"Fix {what}: {acc['name']}",
                summary="; ".join(f"{c['field']}: {c['reason']}" for c in changes) + ".",
                location_slug=loc.slug, account_id=acc["account_id"], confidence=s.confidence,
                actions=[{"op": "update", "account_id": acc["account_id"], "fields": fields}],
                before={k: acc.get(k) for k in fields}, evidence={**ev, "changes": changes},
                fingerprint=fingerprint("update_fields", acc["account_id"], sorted(fields.items()))))

    # The other copies of this facility.
    for d in m.duplicates:
        dacc = d.account
        note = _append_note(dacc.get("note"), f"{MARKER} Duplicate of {acc['account_id']} ({loc.name}): same facility, see {loc.url}")
        billing = (" It has billing history, so its parent is left unchanged and billing can still find it."
                   if (dacc.get("lifetime_revenue") or 0) > 0 or (dacc.get("outstanding_ar") or 0) > 0 else "")
        out.append(Proposal(
            kind="mark_duplicate", title=f"Mark {dacc['name']} as a duplicate",
            summary=f"'{dacc['name']}' ({dacc.get('parent_name') or 'no parent'}) is another copy of {loc.name}. "
                    f"Keep {acc['account_id']} ('{acc['name']}'). {m.survivor_reason}{billing}",
            location_slug=loc.slug, account_id=dacc["account_id"], confidence=d.confidence,
            actions=[{"op": "update", "account_id": dacc["account_id"],
                      "fields": {"duplicate_of_account": acc["account_id"], "status": "Inactive", "note": note}}],
            before={"duplicate_of_account": dacc.get("duplicate_of_account") or "", "status": dacc.get("status"),
                    "note": dacc.get("note") or ""},
            evidence={**_location_evidence(m, d), "survivor": _snapshot(acc), "changes": [
                {"field": "duplicate_of_account", "before": dacc.get("duplicate_of_account") or "", "after": acc["account_id"]},
                {"field": "status", "before": dacc.get("status"), "after": "Inactive"},
                {"field": "note", "before": dacc.get("note") or "", "after": note}]},
            fingerprint=fingerprint("mark_duplicate", dacc["account_id"], acc["account_id"])))
    return out


def _for_orphan(acc: dict, pages: list[str]) -> Proposal | None:
    if acc.get("status") == "Needs Review" and MARKER in (acc.get("note") or ""):
        return None
    note = _append_note(acc.get("note"), f"{MARKER} No longer listed on the Bellhaven website. Ownership unconfirmed: "
                                         "check whether it was sold or closed before changing its parent.")
    branch = sop_branch(acc)
    sop = _sop_evidence(acc, branch)
    caution = (" It has revenue AND outstanding AR, so if it was sold the change of ownership SOP applies."
               if branch == "chow" else "")
    return Proposal(
        kind="flag_not_on_website", title=f"Flag {acc['name']}: not on the website",
        summary=f"'{acc['name']}' is under Bellhaven in the CRM but on no page of the Bellhaven website. We can't tell if it "
                f"was sold or closed, so the parent is left unchanged and the account goes to Needs Review.{caution}",
        location_slug=None, account_id=acc["account_id"], confidence="high",
        actions=[{"op": "update", "account_id": acc["account_id"], "fields": {"status": "Needs Review", "note": note}}],
        before={"status": acc.get("status"), "note": acc.get("note") or ""},
        evidence={"crm": _snapshot(acc), "sop": sop, "pages_checked": pages, "changes": [
            {"field": "status", "before": acc.get("status"), "after": "Needs Review"},
            {"field": "note", "before": acc.get("note") or "", "after": note}]},
        fingerprint=fingerprint("flag_not_on_website", acc["account_id"]))


def build(result: MatchResult, pages_checked: list[str] | None = None) -> list[Proposal]:
    proposals = []
    for m in result.matches:
        proposals.extend(_for_location(m, result.parent))
    for acc in result.orphans:
        p = _for_orphan(acc, pages_checked or [])
        if p:
            proposals.append(p)
    proposals.sort(key=lambda p: (KIND_ORDER.index(p.kind), p.title))
    return proposals


def classify(result: MatchResult, proposals: list[Proposal]) -> list[dict]:
    """One row per website location and per orphan account: the classification shown on the dashboard."""
    by_loc: dict[str, list[str]] = {}
    for p in proposals:
        if p.location_slug:
            by_loc.setdefault(p.location_slug, []).append(p.kind)
    rows = []
    for m in result.matches:
        kinds = by_loc.get(m.location.slug, [])
        if m.survivor is None:
            label = "No CRM account yet"
        elif kinds:
            label = "Match, needs fix"
        else:
            label = "Confident match"
        rows.append({"location": m.location.name, "slug": m.location.slug, "url": m.location.url,
                     "classification": label, "account_id": m.survivor.account["account_id"] if m.survivor else None,
                     "account_name": m.survivor.account["name"] if m.survivor else None,
                     "score": m.survivor.score if m.survivor else None,
                     "confidence": m.survivor.confidence if m.survivor else None,
                     "duplicates": [d.account["account_id"] for d in m.duplicates], "proposals": kinds})
    for acc in result.orphans:
        kinds = [p.kind for p in proposals if p.account_id == acc["account_id"]]
        rows.append({"location": None, "slug": None, "url": None, "classification": "Under Bellhaven, not on website",
                     "account_id": acc["account_id"], "account_name": acc["name"], "score": None, "confidence": None,
                     "duplicates": [], "proposals": kinds})
    return rows
