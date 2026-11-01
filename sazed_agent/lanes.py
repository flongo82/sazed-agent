"""Smistamento nelle due corsie: i comandi passano davanti all'acquisizione dei contenuti."""

from .messages import STATUS_COMMANDS


def lane_for(update):
    """"commands" per il comando di stato in qualunque lingua, "acquisition" per tutto il resto."""
    msg = update.get("message") or update.get("edited_message") or {}
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return "acquisition"
    command = text.split()[0].split("@")[0].lower()
    return "commands" if command in STATUS_COMMANDS else "acquisition"
