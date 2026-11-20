import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from bellhaven_sync.scraper import Location  # noqa: E402

PARENT = {"account_id": "P_BH", "name": "Bellhaven Senior Living (Parent Account)", "parent_id": "", "parent_name": "",
          "billing_street": "", "billing_city": "", "billing_state": "", "billing_zip": "", "care_type": "",
          "status": "Active", "phone": "", "lifetime_revenue": 0, "outstanding_ar": 0,
          "chow_current_account": "", "duplicate_of_account": "", "note": ""}
OTHER_PARENT = {**PARENT, "account_id": "P_CT", "name": "Cedar Trail Communities (Parent Account)"}


def account(account_id, name, street, city, state, zip_, parent=PARENT, **kw):
    a = {**PARENT, "account_id": account_id, "name": name, "billing_street": street, "billing_city": city,
         "billing_state": state, "billing_zip": zip_, "care_type": "Skilled Nursing", "parent_id": parent["account_id"],
         "parent_name": parent["name"]}
    a.update(kw)
    return a


def location(slug, name, street, city, state, zip_, care=("Short-Term Rehabilitation & Nursing",), admin="", phone=""):
    return Location(slug=slug, url=f"https://example.test/communities/{slug}", name=name, street=street, city=city,
                    state=state, zip=zip_, care_offerings=list(care), administrator=admin, phone=phone,
                    found_on=["directory"])


@pytest.fixture
def parent():
    return dict(PARENT)
