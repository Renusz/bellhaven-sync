"""Matching: decide which CRM account(s) are each website location, and which Bellhaven accounts have no location.

Every (location, account) pair gets a score built from independent signals, each worth points:

    address (street + city + zip)  50   the strongest identity signal; formats are normalized first
    administrator is a CRM contact 25   survives rebrands and address typos
    phone                          20
    name similarity                 15   weak on purpose: rebrands change names, and lookalike names lie
    operator email domain           5    contacts @bellhavenliving.com: evidence of ownership
    same city and state             5
    compatible care type            5

A pair is accepted at 45+ points, which in practice means "the address matches" or "two other strong
signals agree". A different state disqualifies outright (that is how "Amberly Manor, Hudson OH" avoids
being matched to the "Amberly Manor" in Colorado). When several accounts are accepted for one location,
they are copies of the same facility: one survivor is kept and the rest are duplicates.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from . import config
from .normalize import (is_po_box, name_similarity, norm_city, norm_phone, norm_state, norm_street,
                        norm_zip, street_number)
from .scraper import Location

MATCH_THRESHOLD = 45
HIGH_CONFIDENCE = 70

POINTS = {"address": 50, "address_zip_differs": 40, "admin": 25, "phone": 20, "name_high": 15, "name_mid": 8,
          "email_domain": 5, "city_state": 5, "care_type": 5}


@dataclass
class Signal:
    name: str
    passed: bool
    points: int
    detail: str


@dataclass
class Candidate:
    account: dict
    score: int
    signals: list[Signal]
    disqualified: str | None = None
    zip_differs: bool = False

    @property
    def confidence(self) -> str:
        return "high" if self.score >= HIGH_CONFIDENCE else "medium"

    def to_dict(self) -> dict:
        return {"account_id": self.account["account_id"], "name": self.account["name"],
                "parent_name": self.account.get("parent_name", ""), "score": self.score,
                "confidence": self.confidence, "disqualified": self.disqualified,
                "signals": [asdict(s) for s in self.signals]}


@dataclass
class LocationMatch:
    location: Location
    accepted: list[Candidate] = field(default_factory=list)
    near_misses: list[Candidate] = field(default_factory=list)
    survivor: Candidate | None = None
    duplicates: list[Candidate] = field(default_factory=list)
    survivor_reason: str = ""


@dataclass
class MatchResult:
    parent: dict
    matches: list[LocationMatch]
    orphans: list[dict]                        # under the target parent, but on no website page
    excluded: list[tuple[dict, str]]           # left out of matching, with the reason


def _contacts_by_account(contacts: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for c in contacts:
        if c.get("is_active", True):
            out.setdefault(c["account_id"], []).append(c)
    return out


def score(loc: Location, acc: dict, contacts: list[dict]) -> Candidate:
    signals: list[Signal] = []
    zip_differs = False

    if norm_state(acc.get("billing_state")) and norm_state(acc.get("billing_state")) != loc.state:
        return Candidate(acc, 0, [Signal("state", False, 0, f"CRM state {acc.get('billing_state')} vs website {loc.state}")],
                         disqualified="different state")

    # Address: street number + normalized street name, then city and zip.
    crm_street = acc.get("billing_street", "")
    same_street = (street_number(crm_street) and street_number(crm_street) == street_number(loc.street)
                   and norm_street(crm_street) == norm_street(loc.street))
    same_city = norm_city(acc.get("billing_city")) == norm_city(loc.city)
    same_zip = norm_zip(acc.get("billing_zip")) == norm_zip(loc.zip)
    if same_street and same_city and same_zip:
        signals.append(Signal("address", True, POINTS["address"], f"'{crm_street}' = '{loc.street}', same city and zip"))
    elif same_street and same_city:
        zip_differs = True
        signals.append(Signal("address", True, POINTS["address_zip_differs"],
                              f"street and city match, zip differs: CRM {acc.get('billing_zip')} vs website {loc.zip}"))
    elif is_po_box(crm_street):
        signals.append(Signal("address", False, 0, f"CRM billing address is a PO box ('{crm_street}'), so the street can't be compared"))
    else:
        signals.append(Signal("address", False, 0, f"'{crm_street}, {acc.get('billing_city')} {acc.get('billing_zip')}' "
                                                   f"vs '{loc.street}, {loc.city} {loc.zip}'"))

    names = {c["name"].strip().lower() for c in contacts}
    admin = (loc.administrator or "").strip().lower()
    if admin and admin in names:
        signals.append(Signal("administrator", True, POINTS["admin"], f"{loc.administrator} is a contact on this account"))
    else:
        signals.append(Signal("administrator", False, 0, f"{loc.administrator or 'none listed'} is not a contact on this account"))

    if norm_phone(acc.get("phone")) and norm_phone(acc.get("phone")) == norm_phone(loc.phone):
        signals.append(Signal("phone", True, POINTS["phone"], f"{acc.get('phone')} matches"))
    else:
        signals.append(Signal("phone", False, 0, f"CRM {acc.get('phone') or 'none'} vs website {loc.phone or 'none'}"))

    sim = name_similarity(acc.get("name"), loc.name, config.OPERATOR_BRAND_WORDS)
    pts = POINTS["name_high"] if sim >= 0.9 else POINTS["name_mid"] if sim >= 0.6 else 0
    signals.append(Signal("name", pts > 0, pts, f"'{acc.get('name')}' vs '{loc.name}' ({sim:.0%} similar)"))

    domain = config.OPERATOR_EMAIL_DOMAIN
    if any((c.get("email") or "").lower().endswith("@" + domain) for c in contacts):
        signals.append(Signal("email domain", True, POINTS["email_domain"], f"has contacts @{domain}"))

    if same_city and norm_state(acc.get("billing_state")) == loc.state:
        signals.append(Signal("city/state", True, POINTS["city_state"], f"{loc.city}, {loc.state}"))

    if acc.get("care_type") and acc.get("care_type") in loc.care_types:
        signals.append(Signal("care type", True, POINTS["care_type"], f"{acc['care_type']} is offered here"))
    elif acc.get("care_type"):
        signals.append(Signal("care type", False, 0, f"CRM {acc['care_type']} vs website {', '.join(loc.care_offerings)}"))

    total = sum(s.points for s in signals)
    return Candidate(acc, total, signals, zip_differs=zip_differs)


def _has_billing_history(acc: dict) -> bool:
    return (acc.get("lifetime_revenue") or 0) > 0 or (acc.get("outstanding_ar") or 0) > 0


def pick_survivor(cands: list[Candidate], parent_id: str, contact_counts: dict[str, int]) -> tuple[Candidate, str]:
    """Which copy of a duplicated facility to keep. Deterministic, so re-runs pick the same one.

    Order: already under the right parent, then has billing history (billing references it), then has
    contacts, then the higher match score, then the lowest account id as a stable tie-break.
    """
    def key(c: Candidate):
        a = c.account
        return (a.get("parent_id") == parent_id, _has_billing_history(a), contact_counts.get(a["account_id"], 0),
                c.score, [-ord(ch) for ch in a["account_id"]])

    ranked = sorted(cands, key=key, reverse=True)
    best, runner = ranked[0], ranked[1]
    kb, kr = key(best), key(runner)
    reasons = ["it is already under the right parent", "it has billing history", "it has more CRM contacts",
               "it matches the website on more signals"]
    for reason, x, y in zip(reasons, kb, kr):
        if x != y:
            return best, f"Kept this copy because {reason}."
    return best, ("All copies tie on parent, billing history, contacts and match score, so the lowest account id "
                  "is kept. The rule is deterministic, so every re-run picks the same survivor.")


def match(locations: list[Location], accounts: list[dict], contacts: list[dict], parent_name: str | None = None) -> MatchResult:
    parent_name = parent_name or config.TARGET_PARENT_NAME
    parents = [a for a in accounts if a["name"] == parent_name]
    if len(parents) != 1:
        raise RuntimeError(f"Expected exactly one '{parent_name}' account, found {len(parents)}")
    parent = parents[0]

    parent_ids = {a["parent_id"] for a in accounts if a.get("parent_id")}
    by_acc = _contacts_by_account(contacts)
    contact_counts = {k: len(v) for k, v in by_acc.items()}

    pool, excluded = [], []
    for a in accounts:
        if a["account_id"] in parent_ids or a["name"].endswith("(Parent Account)"):
            excluded.append((a, "parent company account"))
        elif a.get("duplicate_of_account"):
            excluded.append((a, f"already marked a duplicate of {a['duplicate_of_account']}"))
        elif a.get("chow_current_account"):
            excluded.append((a, f"kept for billing after a change of ownership; current account is {a['chow_current_account']}"))
        else:
            pool.append(a)

    # Score every pair. 35 locations x ~120 accounts is small enough to skip blocking.
    scored = {loc.slug: [score(loc, a, by_acc.get(a["account_id"], [])) for a in pool] for loc in locations}

    # One account belongs to at most one location: give each accepted account to its best-scoring location.
    best_for_account: dict[str, tuple[int, str]] = {}
    for slug, cands in scored.items():
        for c in cands:
            if not c.disqualified and c.score >= MATCH_THRESHOLD:
                aid = c.account["account_id"]
                if aid not in best_for_account or c.score > best_for_account[aid][0]:
                    best_for_account[aid] = (c.score, slug)

    matches = []
    for loc in locations:
        cands = scored[loc.slug]
        accepted = [c for c in cands if best_for_account.get(c.account["account_id"], (0, None))[1] == loc.slug]
        accepted.sort(key=lambda c: -c.score)
        # Lookalikes we considered and rejected, kept as evidence (e.g. same name in another state).
        rejected = [c for c in cands if c not in accepted and (c.score >= 15 or c.disqualified and
                    name_similarity(c.account["name"], loc.name, config.OPERATOR_BRAND_WORDS) >= 0.75)]
        rejected.sort(key=lambda c: (-name_similarity(c.account["name"], loc.name, config.OPERATOR_BRAND_WORDS), -c.score))
        m = LocationMatch(location=loc, accepted=accepted, near_misses=rejected[:3])
        if len(accepted) == 1:
            m.survivor = accepted[0]
        elif len(accepted) > 1:
            m.survivor, m.survivor_reason = pick_survivor(accepted, parent["account_id"], contact_counts)
            m.duplicates = [c for c in accepted if c is not m.survivor]
        matches.append(m)

    matched_ids = {c.account["account_id"] for m in matches for c in m.accepted}
    orphans = [a for a in pool if a.get("parent_id") == parent["account_id"] and a["account_id"] not in matched_ids]
    return MatchResult(parent=parent, matches=matches, orphans=orphans, excluded=excluded)
