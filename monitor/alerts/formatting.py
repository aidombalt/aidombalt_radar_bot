"""Backward-compatible Telegram/mail API; content is shared with MAX in render.py."""
from .render import (PRIO_ICON, PRIO_NAME, LEVEL, STATE_KB_OPEN, objects_line, link_label,
                     email_alert_html, _alert_html, _keyboard_data, _card_data)


def tg_alert(*args, **kwargs):
    return _alert_html(*args, **kwargs)


def tg_keyboard(*args, **kwargs):
    return _keyboard_data(*args, **kwargs)


def tg_card(*args, **kwargs):
    return _card_data(*args, **kwargs)
