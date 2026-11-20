from conftest import OTHER_PARENT, PARENT, account, location

from bellhaven_sync import matching, proposals


def run(locs, accounts, contacts=()):
    result = matching.match(locs, [PARENT, OTHER_PARENT, *accounts], list(contacts))
    return result, proposals.build(result)


def kinds(props):
    return sorted(p.kind for p in props)


def test_confident_match_with_different_address_spelling_needs_nothing():
    loc = location("defiance", "Bellhaven of Defiance", "700 Carpenter Rd", "Defiance", "OH", "43512", phone="(330) 691-2711")
    acc = account("A1", "Bellhaven of Defiance", "700 Carpenter Road", "Defiance", "OH", "43512", phone="330-691-2711")
    result, props = run([loc], [acc])
    assert result.matches[0].survivor.account["account_id"] == "A1"
    assert props == []


def test_same_name_in_another_state_is_not_a_match():
    loc = location("amberly-manor", "Amberly Manor", "4390 Darrow Rd", "Hudson", "OH", "44236", care=("Assisted Living",))
    lookalike = account("A2", "Amberly Manor", "918 S Nevada Ave", "Colorado Springs", "CO", "80903",
                        parent=OTHER_PARENT, care_type="Assisted Living")
    result, props = run([loc], [lookalike])
    assert result.matches[0].survivor is None
    assert kinds(props) == ["create_account"]
    assert result.matches[0].near_misses[0].disqualified == "different state"


def test_po_box_account_matches_on_administrator_and_phone():
    loc = location("ashtabula", "Bellhaven of Ashtabula", "3156 W Prospect Rd", "Ashtabula", "OH", "44004",
                   admin="Ned Green", phone="(260) 440-4975")
    acc = account("A3", "Bellhaven of Ashtabula", "PO Box 517", "Ashtabula", "OH", "44004", phone="(260) 440-4975")
    contacts = [{"contact_id": "C1", "account_id": "A3", "name": "Ned Green", "email": "", "is_active": True}]
    result, props = run([loc], [acc], contacts)
    assert result.matches[0].survivor.account["account_id"] == "A3"
    assert props == []  # the PO box is a billing address, so we don't overwrite it


def test_zip_typo_is_fixed():
    loc = location("portsmouth", "Bellhaven of Portsmouth", "2222 Gallia St", "Portsmouth", "OH", "45662")
    acc = account("A4", "Bellhaven of Portsmouth", "2222 Gallia St", "Portsmouth", "OH", "45626")
    _, props = run([loc], [acc])
    assert kinds(props) == ["update_fields"]
    assert props[0].actions[0]["fields"] == {"billing_zip": "45662"}


def test_rebrand_under_wrong_parent_without_billing_is_reparented_and_renamed():
    loc = location("zanesville", "Bellhaven of Zanesville", "2680 Maple Ave", "Zanesville", "OH", "43701")
    acc = account("A5", "Cedar Trail of Zanesville", "2680 Maple Avenue", "Zanesville", "OH", "43701", parent=OTHER_PARENT)
    _, props = run([loc], [acc])
    assert kinds(props) == ["reparent", "update_fields"]
    reparent = next(p for p in props if p.kind == "reparent")
    assert reparent.actions == [{"op": "update", "account_id": "A5", "fields": {"parent_id": "P_BH"}}]


def test_sop_revenue_and_ar_means_change_of_ownership_and_old_account_untouched():
    loc = location("tiffin", "Bellhaven of Tiffin", "45 St Lawrence Dr", "Tiffin", "OH", "44883")
    acc = account("A6", "Tiffin Care", "45 St Lawrence Dr", "Tiffin", "OH", "44883", parent=OTHER_PARENT,
                  lifetime_revenue=84000, outstanding_ar=12400)
    _, props = run([loc], [acc])
    assert kinds(props) == ["chow"]  # no rename on the old account: the SOP says leave it exactly as it is
    create, update = props[0].actions
    assert create["op"] == "create" and create["fields"]["parent_id"] == "P_BH" and create["fields"]["name"] == "Bellhaven of Tiffin"
    assert update == {"op": "update", "account_id": "A6", "fields": {"chow_current_account": proposals.NEW_ID}}


def test_sop_branches():
    assert proposals.sop_branch({"lifetime_revenue": 1, "outstanding_ar": 1}) == "chow"
    assert proposals.sop_branch({"lifetime_revenue": 47000, "outstanding_ar": 0}) == "reparent"
    assert proposals.sop_branch({"lifetime_revenue": 0, "outstanding_ar": 500}) == "reparent"
    assert proposals.sop_branch({"lifetime_revenue": 0, "outstanding_ar": 0}) == "reparent"


def test_duplicates_keep_the_copy_under_the_right_parent():
    loc = location("monroe", "Bellhaven Gardens of Monroe", "750 Stewart Rd", "Monroe", "MI", "48162", care=("Assisted Living",))
    keep = account("B1", "Bellhaven Gardens of Monroe", "750 Stewart Rd", "Monroe", "MI", "48162", care_type="Assisted Living")
    dup = account("A9", "Cedar Trail of Monroe", "750 Stewart Road", "Monroe", "MI", "48162", parent=OTHER_PARENT,
                  care_type="Assisted Living")
    result, props = run([loc], [keep, dup])
    assert result.matches[0].survivor.account["account_id"] == "B1"
    assert kinds(props) == ["mark_duplicate"]
    fields = props[0].actions[0]["fields"]
    assert fields["duplicate_of_account"] == "B1" and fields["status"] == "Inactive"


def test_account_under_bellhaven_missing_from_website_is_flagged_not_moved():
    orphan = account("A7", "Bellhaven of Sandusky", "2715 Columbus Ave", "Sandusky", "OH", "44870",
                     lifetime_revenue=130000, outstanding_ar=5200)
    _, props = run([], [orphan])
    assert kinds(props) == ["flag_not_on_website"]
    assert props[0].actions[0]["fields"]["status"] == "Needs Review"
    assert "parent_id" not in props[0].actions[0]["fields"]


def test_resolved_accounts_are_not_proposed_again():
    loc = location("tiffin", "Bellhaven of Tiffin", "45 St Lawrence Dr", "Tiffin", "OH", "44883")
    old = account("A6", "Bellhaven of Tiffin", "45 St Lawrence Dr", "Tiffin", "OH", "44883", parent=OTHER_PARENT,
                  lifetime_revenue=84000, outstanding_ar=12400, chow_current_account="N1")
    new = account("N1", "Bellhaven of Tiffin", "45 St Lawrence Dr", "Tiffin", "OH", "44883")
    dup = account("D1", "Old Tiffin copy", "45 St Lawrence Dr", "Tiffin", "OH", "44883", duplicate_of_account="N1", status="Inactive")
    result, props = run([loc], [old, new, dup])
    assert result.matches[0].survivor.account["account_id"] == "N1"
    assert props == []
