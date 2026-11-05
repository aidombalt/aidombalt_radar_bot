"""Доставка (пилот 24.09): «unsupported parse_mode», ложный 🔴 в статусе, отчёт из бота через релей, немые сбои."""
import asyncio
import json

import httpx

from monitor.util import iso


class TgFake:
    """Подмена api.telegram.org: записывает запросы и отвечает по сценарию."""

    def __init__(self, answers):
        self.answers, self.sent = list(answers), []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(json.loads(request.content))
        code, desc = self.answers.pop(0) if self.answers else (200, "")
        body = {"ok": True, "result": {"message_id": 5}} if code == 200 else {"ok": False, "description": desc}
        return httpx.Response(code, json=body)


def _alerts(ctx, fake):
    ctx.alerts._http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    return ctx.alerts


def test_bad_html_is_resent_as_plain_text(ctx, monkeypatch):
    monkeypatch.setenv("TG_BOT_TOKEN", "1:x")
    fake = TgFake([(400, "Bad Request: can't parse entities: unsupported start tag \"улица\""), (200, "")])
    al = _alerts(ctx, fake)
    asyncio.run(al.send_tg({"chat_id": 1, "text": "<b>Источник</b> упал: ответ <улица> & дом", "parse_mode": "HTML"}))
    retry = fake.sent[1]
    assert "parse_mode" not in retry                                   # null Telegram не принимает — ключа нет вовсе
    assert retry["text"] == "Источник упал: ответ  & дом"


def test_rejected_message_is_not_a_network_failure(ctx, monkeypatch):
    monkeypatch.setenv("TG_BOT_TOKEN", "1:x")
    fake = TgFake([(400, "Bad Request: message is too long")] * 3)
    al = _alerts(ctx, fake)
    al.enqueue("telegram", "111", {"chat_id": "111", "text": "служебное", "parse_mode": "HTML"}, "adm:x:111")
    asyncio.run(al.process())
    row = ctx.db.one("SELECT status, attempts FROM outbox WHERE dedup_key='adm:x:111'")
    assert row["status"] == "dead" and row["attempts"] == 1           # не повторяем и не шлём в ретранслятор
    assert al.tg_ok is not False                                        # связь в порядке — статус не краснеет
    fake2 = TgFake([(200, "")])
    al._http = httpx.AsyncClient(transport=httpx.MockTransport(fake2.handler))
    al.enqueue("telegram", "111", {"chat_id": "111", "text": "x" * 5000}, "adm:long:111")
    asyncio.run(al.process())
    assert len(fake2.sent[0]["text"]) <= 4000


def test_bot_report_via_relay_goes_by_mail(ctx, monkeypatch):
    from aiogram.methods import EditMessageText, SendDocument, SendMessage

    from monitor.alerts.bot import build_bot
    from monitor.reports import builder
    from monitor.service import Service
    from tests.test_bot import MockSession, msg

    monkeypatch.setenv("RELAY_URL", "https://relay.example/KEY")
    ctx.db.x("UPDATE users SET email='op@example.ru' WHERE tg_id='111'")

    async def fake_build(ctx_, **kw):
        return {"id": 7, "xlsx": "/tmp/r.xlsx", "docx": "/tmp/r.docx", "items": 3, "narrative_by": "шаблон",
                "short": "📊 <b>Отчёт</b>\n✅ Упоминаний наших объектов нет.", "subject": "Радар: тишина", "html": "<p>ok</p>"}
    monkeypatch.setattr(builder, "build", fake_build)
    sess = MockSession()
    bot, dp = build_bot(ctx, Service(ctx), session=sess, token="123456:TEST-TOKEN-abcdefghijklmnopqrstuvwxyz")
    asyncio.run(dp.feed_update(bot, msg("/report")))
    texts = [m.text for m in sess.sent if isinstance(m, (SendMessage, EditMessageText))]
    assert not [m for m in sess.sent if isinstance(m, SendDocument)]   # через релей файлы не грузим — обрываются
    assert len([m for m in sess.sent if isinstance(m, SendMessage)]) == 1
    assert any("Упоминаний наших объектов нет" in t for t in texts)
    assert any("отправил на почту" in t and "op@example.ru" in t for t in texts)
    mail = ctx.db.one("SELECT payload_json FROM outbox WHERE channel='email' AND dedup_key='report:7:op@example.ru'")
    assert mail and "/tmp/r.xlsx" in mail["payload_json"]


def test_bot_network_error_is_explained(ctx, monkeypatch):
    from aiogram.exceptions import TelegramNetworkError
    from aiogram.methods import SendMessage

    from monitor.alerts.bot import build_bot
    from monitor.service import Service
    from tests.test_bot import MockSession, msg

    class Flaky(MockSession):
        broke = False

        async def make_request(self, bot, method, timeout=None):
            if isinstance(method, SendMessage) and "состояние" in (method.text or "") and not self.broke:
                self.broke = True
                raise TelegramNetworkError(method=method, message="ClientOSError: [WinError 121] Превышен таймаут семафора")
            return await super().make_request(bot, method, timeout)

    sess = Flaky()
    bot, dp = build_bot(ctx, Service(ctx), session=sess, token="123456:TEST-TOKEN-abcdefghijklmnopqrstuvwxyz")
    asyncio.run(dp.feed_update(bot, msg("/status")))
    last = [m.text for m in sess.sent if isinstance(m, SendMessage)][-1]
    assert "оборвалась" in last


def test_status_line_is_honest(ctx, monkeypatch):
    from aiogram.methods import SendMessage

    from monitor.alerts.bot import build_bot
    from monitor.service import Service
    from tests.test_bot import MockSession, msg

    monkeypatch.setenv("RELAY_URL", "https://relay.example/KEY")
    sess = MockSession()
    bot, dp = build_bot(ctx, Service(ctx), session=sess, token="123456:TEST-TOKEN-abcdefghijklmnopqrstuvwxyz")
    ctx.alerts.tg_ok, ctx.alerts.tg_ok_at = True, iso()
    asyncio.run(dp.feed_update(bot, msg("/status")))
    st = [m.text for m in sess.sent if isinstance(m, SendMessage)][-1]
    assert "🟢 доходят (через релей)" in st and "🔴" not in st.split("Алерты")[1]
