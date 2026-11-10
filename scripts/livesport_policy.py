"""Explicit owner-requested pause (2026-11-10); no environment override.

Parsing saved data and preserving prediction archives remain allowed. Re-enabling
network collection requires a deliberate code change after source permission is
resolved, not merely re-enabling a GitHub workflow.
"""
from urllib.parse import urlsplit

LIVESPORT_IMPORTS_PAUSED = True
PAUSE_MESSAGE = "Livesport imports are paused by owner request. Existing data and predictions are preserved."


def require_livesport_enabled():
    if LIVESPORT_IMPORTS_PAUSED:
        raise RuntimeError(PAUSE_MESSAGE)


def ensure_fetch_allowed(url):
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    domains = ("livesport.cz", "livesport.com", "livesport.eu", "flashscore.com", "lsapp.eu")
    if any(host == domain or host.endswith("." + domain) for domain in domains):
        require_livesport_enabled()
