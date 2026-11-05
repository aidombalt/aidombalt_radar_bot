"""Почта (SMTP): письма с вложениями. Работает в отдельном потоке, чтобы не блокировать цикл."""
from __future__ import annotations

import asyncio
import mimetypes
import smtplib
import ssl
import html as htmllib
import re
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path


def _send(cfg: dict, to: list[str], subject: str, html: str, attachments: list[str] | None = None) -> None:
    if not cfg.get("host") or not to:
        raise RuntimeError("SMTP не настроен или нет получателей")
    msg = EmailMessage()
    sender = cfg["from"]
    # Date, Message-ID и настоящий текстовый вариант: без них почтовики (Яндекс, Gmail) считают письмо спамом
    msg["From"] = formataddr(("Радар мониторинга", sender))
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1] if "@" in sender else None)
    msg.set_content(html_to_text(html))
    msg.add_alternative(html, subtype="html")
    for p in attachments or []:
        path = Path(p)
        if not path.exists():
            continue
        ctype, _ = mimetypes.guess_type(path.name)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        msg.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name)
    ctx = ssl.create_default_context()
    if cfg.get("starttls") or cfg["port"] == 587:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=30) as s:
            s.starttls(context=ctx)
            if cfg.get("user"):
                s.login(cfg["user"], cfg["password"])
            s.send_message(msg)
    else:
        with smtplib.SMTP_SSL(cfg["host"], cfg["port"], context=ctx, timeout=30) as s:
            if cfg.get("user"):
                s.login(cfg["user"], cfg["password"])
            s.send_message(msg)


def html_to_text(html: str) -> str:
    t = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    t = re.sub(r"(?i)<br\s*/?>|</p>|</h\d>|</tr>|</li>|</div>", "\n", t)
    t = htmllib.unescape(re.sub(r"<[^>]+>", "", t))
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", t)).strip() or " "


async def send_email(cfg: dict, to: list[str], subject: str, html: str, attachments: list[str] | None = None) -> None:
    await asyncio.to_thread(_send, cfg, to, subject, html, attachments)
