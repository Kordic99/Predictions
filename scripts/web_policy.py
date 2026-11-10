"""Owner-requested disconnection of every data integration, 2026-11-10."""
WEB_CONNECTIONS_PAUSED = True
PAUSE_MESSAGE = "All web connections are paused by owner request. Saved data is preserved."


def require_web_enabled():
    if WEB_CONNECTIONS_PAUSED:
        raise RuntimeError(PAUSE_MESSAGE)
