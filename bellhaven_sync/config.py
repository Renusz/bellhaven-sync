"""Settings, read from environment variables. A local `.env` file is loaded if present (never committed)."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

SITE_URL = os.environ.get("SITE_URL", "https://analyst-assessment-production.up.railway.app").rstrip("/")
CRM_BASE_URL = os.environ.get("CRM_BASE_URL", SITE_URL + "/api/v1").rstrip("/")
CRM_TOKEN = os.environ.get("CRM_TOKEN", "")
STATE_DB = Path(os.environ.get("STATE_DB", str(ROOT / "state" / "bellhaven_sync.db")))

# The operator whose website we sync, and the CRM parent account its facilities belong under.
TARGET_PARENT_NAME = os.environ.get("TARGET_PARENT_NAME", "Bellhaven Senior Living (Parent Account)")
OPERATOR_EMAIL_DOMAIN = os.environ.get("OPERATOR_EMAIL_DOMAIN", "bellhavenliving.com")
# Brand words ignored when comparing facility names ("Bellhaven of X" vs "Bellhaven of Y" are not alike).
OPERATOR_BRAND_WORDS = frozenset(os.environ.get("OPERATOR_BRAND_WORDS", "bellhaven").lower().split(","))

# Website care offerings -> the CRM's care_type vocabulary.
CARE_TYPE_MAP = {
    "short-term rehabilitation & nursing": "Skilled Nursing",
    "short-term rehabilitation and nursing": "Skilled Nursing",
    "skilled nursing": "Skilled Nursing",
    "memory support": "Memory Care",
    "memory care": "Memory Care",
    "assisted living": "Assisted Living",
    "independent living": "Independent Living",
}

USER_AGENT = "bellhaven-sync/1.0 (CRM ownership sync)"


def crm_browser_url(account_id: str | None = None) -> str:
    """Link to the read-only CRM browser (the sandbox puts the token in the path)."""
    if not CRM_TOKEN:
        return ""
    base = f"{SITE_URL}/crm/{CRM_TOKEN}/accounts"
    return f"{base}/{account_id}" if account_id else base
