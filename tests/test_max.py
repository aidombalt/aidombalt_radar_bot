"""Contract and integration tests for MAX; no real API tokens or external network."""
import asyncio
import json
import logging
import socket
import sqlite3
import ssl
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from lxml import etree

from monitor import workhours
from monitor.alerts.commands import Actor, handle_action, link_code, redeem_link, command
from monitor.alerts.max_api import MaxAPI, MaxError
from monitor.alerts.max_bot import MaxBot, make_router
from monitor.alerts.render import Card, Btn, card_for, safe_html
from monitor.alerts import formatting, subscriptions
from monitor.db import DB
from monitor.netguard import LocalOnly
from monitor.util import iso, ago, jdump, jload, now_utc
from tests.test_foreign_house import post, NEWS47, DTP_KUDROVO, DTP_NOADDR


class FakeMax:
    def __init__(self):
        self.calls = []
        self.errors = {}
        self.events = []
        self.subs = []
        self.n = 0
        self.permissions = ["read_all_messages", "write", "pin_message"]
        self.pinned = None

    def handler(self, request):
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content and 'json' in request.headers.get('content-type', '') else None
        self.calls.append((method, path, dict(request.url.params), body, dict(request.headers)))
        errors = self.errors.get((method, path), [])
        if errors:
            status, code, *headers = errors.pop(0)
            return httpx.Response(status, json={"code": code, "message": "sensitive remote details"}, headers=headers[0] if headers else {})
        result = {"success": True}
        if method == "POST" and path == "/messages":
            self.n += 1
            result = {"message": {"body": {"mid": f"mid.{self.n}"}, "recipient": {"chat_id": -77, "chat_type": "chat"}}}
        elif path == "/me":
            result = {"user_id": 99, "is_bot": True, "first_name": "Radar"}
        elif path == "/chats/-77":
            result = {"chat_id": -77, "type": "chat", "title": "Дежурные"}
        elif path == "/chats/-77/members/me":
            result = {"user_id": 99, "is_admin": True, "is_owner": False, "permissions": self.permissions}
        elif path == "/updates":
            result = {"updates": self.events, "marker": 987}
        elif path == "/subscriptions":
            if method == "GET":
                result = {"subscriptions": self.subs}
            elif method == "POST":
                self.subs = [body]
            else:
                self.subs = [x for x in self.subs if x['url'] != request.url.params['url']]
        elif path.endswith("/pin"):
            if method == "PUT": self.pinned = body['message_id']
            elif method == "DELETE": self.pinned = None
            else: result = {"message": {"body": {"mid": self.pinned}}} if self.pinned else {}
        elif path == "/uploads":
            result = {"url": "https://upload.max.ru/file"}
        elif path == "/file":
            result = {"token": "uploaded-token"}
        return httpx.Response(200, json=result)

    def sent(self):
        return [c for c in self.calls if c[:2] == ("POST", "/messages")]


@pytest.fixture
def mx(ctx, monkeypatch):
    monkeypatch.setenv("MAX_BOT_TOKEN", "test-max-token")
    monkeypatch.setenv("MAX_ADMIN_CHAT_ID", "-77")
    monkeypatch.setenv("TG_BOT_TOKEN", "123:test")
    ctx.settings.alerts.night_hold = False
    ctx.db.x("UPDATE users SET max_user_id='501' WHERE tg_id='111'")
    fake = FakeMax()
    ctx.alerts.max._api = MaxAPI("test-max-token", transport=httpx.MockTransport(fake.handler))
    ctx.alerts._http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True, "result": {"message_id": 20}})))
    return fake


def item(ctx):
    return post(ctx, "ЖК Виктория, Авиаторов Балтики 9к1 — лифт стоит вторую неделю, УК молчит! Александр Юрьевич, помогите!")


def callback(iid, cid="cb1", uid=501, action="ack", chat=-77, timestamp=None):
    return {"update_type": "message_callback", "timestamp": timestamp or int(time.time()*1000),
            "callback": {"callback_id": cid, "payload": f"a:{action}:{iid}", "timestamp": int(time.time()*1000),
                         "user": {"user_id": uid, "first_name": "Name from MAX", "is_bot": False}},
            "message": {"sender": {"user_id": 99, "is_bot": True}, "recipient": {"chat_id": chat, "chat_type": "chat"},
                        "timestamp": int(time.time()*1000), "body": {"mid": "mid.1", "seq": 1, "text": "card"}}}


def message(text, uid=501, chat=-77, private=False, mid="incoming1", age=0):
    return {"update_type": "message_created", "timestamp": int((time.time()-age)*1000),
            "message": {"sender": {"user_id": uid, "is_bot": False, "first_name": "Operator"},
                        "recipient": {"chat_id": chat, "chat_type": "dialog" if private else "chat"},
                        "timestamp": int((time.time()-age)*1000), "body": {"mid": mid, "seq": 1, "text": text}}}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["bot_started", "message_created"])
async def test_first_private_start_explains_link_without_granting_access(ctx, mx, kind):
    event = {"update_type": "bot_started", "timestamp": int(time.time()*1000),
             "chat_id": 700, "user": {"user_id": 700, "is_bot": False}}
    if kind == "message_created":
        event = message('/start', uid=700, chat=700, private=True)
    await ctx.max_bot.handle(event)
    await ctx.alerts.max.process()
    assert len(mx.sent()) == 1
    response = mx.sent()[0]
    assert response[2] == {"user_id": "700"}
    assert '/link' in response[3]['text'] and '700' in response[3]['text']
    assert not ctx.db.one("SELECT 1 FROM users WHERE max_user_id='700'")
    await ctx.max_bot.handle(message('/status', uid=700, chat=700, private=True, mid='status'))
    await ctx.alerts.max.process()
    assert 'Нет доступа' in mx.sent()[-1][3]['text']


@pytest.mark.asyncio
async def test_command_menu_failure_does_not_stop_polling(ctx, mx, caplog, monkeypatch):
    mx.errors[('PATCH', '/me/commands')] = [(400, 'commands.invalid')]
    polled = asyncio.Event()

    async def poll():
        polled.set()
        await asyncio.Future()

    monkeypatch.setattr(ctx.max_bot, 'poll_once', poll)
    caplog.set_level(logging.INFO, logger='radar.max')
    task = asyncio.create_task(ctx.max_bot.run())
    try:
        await asyncio.wait_for(polled.wait(), 2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert 'commands.invalid' in caplog.text
    assert 'test-max-token' not in caplog.text


@pytest.mark.asyncio
async def test_valid_token_resumes_private_replies_without_group(ctx, mx, monkeypatch):
    ctx.alerts.max.pause(MaxError(401, 'unauthorized'))
    monkeypatch.delenv('MAX_ADMIN_CHAT_ID')
    await ctx.max_bot.check_health()
    await ctx.max_bot.handle(message('/start', private=True, chat=501))
    await ctx.alerts.max.process()
    assert mx.sent()
    assert '*' not in ctx.db.kv_get('max_paused', {})


@pytest.mark.asyncio
async def test_disabled_private_start_does_not_enable_access(ctx, mx):
    ctx.settings.max.direct_messages = False
    await ctx.max_bot.handle(message('/start', uid=700, chat=700, private=True))
    await ctx.max_bot.handle({'update_type': 'bot_started', 'timestamp': int(time.time()*1000),
                              'chat_id': 700, 'user': {'user_id': 700}})
    await ctx.alerts.max.process()
    assert not mx.sent()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure,expected', [
    (ssl.SSLCertVerificationError(1, 'CERTIFICATE_VERIFY_FAILED secret-test-token'), 'tls.verify.failed'),
    (socket.gaierror(-2, 'secret-test-token'), 'network.dns.failed'),
    (httpx.ReadTimeout('secret-test-token'), 'network.timeout'),
])
async def test_connection_errors_are_diagnostic_without_leaking_secrets(failure, expected):
    def fail(request):
        if isinstance(failure, httpx.HTTPError):
            raise failure
        raise httpx.ConnectError('secret-test-token', request=request) from failure

    api = MaxAPI('secret-test-token', transport=httpx.MockTransport(fail))
    try:
        with pytest.raises(MaxError) as caught:
            await api.me()
        assert caught.value.code == expected
        assert 'secret-test-token' not in str(caught.value)
    finally:
        await api.close()


@pytest.mark.asyncio
async def test_maxcheck_reads_only_and_hides_token_and_webhook_secret(ctx, mx, capsys):
    from monitor.alerts.max_check import check

    mx.subs = [{'url': 'https://private.example/secret-webhook'}]
    assert await check(ctx)
    output = capsys.readouterr().out
    assert 'Max принял токен' in output and 'Webhook-подписок: 1' in output
    assert 'test-max-token' not in output and 'secret-webhook' not in output
    assert all(c[0] == 'GET' and c[1] != '/updates' for c in mx.calls)
    assert not ctx.db.q('SELECT * FROM outbox')


@pytest.mark.asyncio
async def test_missing_token_is_logged_without_network(ctx, monkeypatch, caplog):
    monkeypatch.delenv('MAX_BOT_TOKEN', raising=False)
    await ctx.max_bot.run()
    assert 'MAX_BOT_TOKEN не загружен' in caplog.text
    assert ctx.alerts.max._api is None


@pytest.mark.asyncio
async def test_health_failure_is_visible_in_console(ctx, mx, caplog):
    mx.errors[('GET', '/me')] = [(401, 'access.denied')]
    assert not await ctx.max_bot.check_health()
    assert 'MAX HTTP 401' in caplog.text
    assert 'test-max-token' not in caplog.text


@pytest.mark.asyncio
async def test_full_polling_start_dispatch_delivery(ctx, mx, monkeypatch):
    mx.events = [{'update_type': 'bot_started', 'timestamp': int(time.time()*1000),
                  'chat_id': 700, 'user': {'user_id': 700}}]
    delivered = asyncio.Event()
    original_poll = ctx.max_bot.poll_once

    async def poll_and_deliver():
        assert await original_poll()
        await ctx.alerts.max.process()
        delivered.set()
        await asyncio.Future()

    monkeypatch.setattr(ctx.max_bot, 'poll_once', poll_and_deliver)
    task = asyncio.create_task(ctx.max_bot.run())
    try:
        await asyncio.wait_for(delivered.wait(), 2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert len(mx.sent()) == 1 and '/link' in mx.sent()[0][3]['text']
    assert ctx.db.scalar("SELECT status FROM max_events") == 'done'
    assert ctx.db.kv_get('max_marker') == 987


@pytest.mark.asyncio
async def test_p1_exactly_one_card_with_header_keyboard_and_string_mid(ctx, mx):
    iid = item(ctx)
    ctx.alerts.raise_alert(iid, "P1")
    await ctx.alerts.max.process()
    ctx.alerts.raise_alert(iid, "P1")
    await ctx.alerts.max.process()
    assert len(mx.sent()) == 1
    request = mx.sent()[0]
    assert request[2] == {"chat_id": "-77"}
    assert request[4]['authorization'] == 'test-max-token'
    assert 'test-max-token' not in str(request[2])
    body = request[3]
    assert len(body['text']) <= 4000
    rows = body['attachments'][0]['payload']['buttons']
    assert all(len(row) <= 3 for row in rows)
    assert any(b.get('payload') == f'a:ack:{iid}' for row in rows for b in row)
    assert ctx.db.scalar("SELECT mid FROM max_msgs WHERE item_id=?", (iid,)) == "mid.1"
    assert ctx.alerts.max.api.http._trust_env is False


@pytest.mark.asyncio
async def test_max_action_cross_channel_callback_answer_duplicate_and_last_wins(ctx, mx):
    iid = item(ctx)
    await ctx.alerts.process()
    tg = []
    async def fake_tg(method, payload):
        tg.append((method, payload)); return {}
    ctx.alerts._tg = fake_tg
    ev = callback(iid)
    await ctx.max_bot.handle(ev)
    assert ctx.db.item(iid)['workflow'] == 'in_work'
    assert any(x[0] == 'editMessageText' and 'Max' in x[1]['text'] for x in tg)
    assert any(c[:2] == ('POST','/answers') and 'message' in c[3] for c in mx.calls)
    await ctx.max_bot.handle(ev)
    assert ctx.db.scalar("SELECT count(*) FROM feedback WHERE item_id=? AND verdict='in_work'", (iid,)) == 1
    await ctx.max_bot.handle(callback(iid, 'cb2', action='fp'))
    assert ctx.db.item(iid)['workflow'] == 'false_positive'
    answers = [c[3]['notification'] for c in mx.calls if c[:2] == ('POST','/answers')]
    assert any('Уже отмечено:' in x and 'Оператор' in x for x in answers)


@pytest.mark.asyncio
async def test_telegram_real_dispatcher_action_edits_max(ctx, mx):
    from tests.test_bot import MockSession, NOW, CHAT
    from monitor.alerts.bot import build_bot
    from monitor.service import Service
    from aiogram.types import CallbackQuery, Message, Update, User
    iid = item(ctx)
    await ctx.alerts.max.process()
    session = MockSession()
    bot, dp = build_bot(ctx, Service(ctx), session=session, token='123456:TEST-TOKEN-abcdefghijklmnopqrstuvwxyz')
    event = Update(update_id=8, callback_query=CallbackQuery(id='tg1', from_user=User(id=111,is_bot=False,first_name='Иванов'),
                      chat_instance='x', data=f'a:res:{iid}', message=Message(message_id=20,date=NOW,chat=CHAT,text='card')))
    await dp.feed_update(bot, event)
    edits = [c for c in mx.calls if c[:2] == ('PUT','/messages')]
    assert edits and 'Решено' in edits[-1][3]['text'] and 'Telegram' in edits[-1][3]['text']


@pytest.mark.asyncio
@pytest.mark.parametrize('uid,role', [(900,None),(501,'viewer')])
async def test_access_denied_without_mutation(ctx, mx, uid, role):
    iid = item(ctx)
    if role: ctx.db.x("UPDATE users SET role=?", (role,))
    await ctx.max_bot.handle(callback(iid, uid=uid))
    assert ctx.db.item(iid)['workflow'] == 'open'
    assert ctx.db.scalar('SELECT COUNT(*) FROM feedback') == 0
    assert any(c[:2] == ('POST','/answers') for c in mx.calls)


@pytest.mark.asyncio
async def test_foreign_group_and_null_message_do_not_mutate(ctx, mx):
    iid = item(ctx)
    await ctx.max_bot.handle(callback(iid, chat=-999))
    event = callback(iid,'deleted');event['message'] = None
    await ctx.max_bot.handle(event)
    assert ctx.db.item(iid)['workflow'] == 'open'
    await ctx.max_bot.handle(message('/status',chat=-999))
    assert not ctx.db.one("SELECT 1 FROM outbox WHERE dedup_key LIKE 'maxreply:%'")
    await ctx.max_bot.handle(message('/chatid',chat=-999,mid='chatid'))
    row = ctx.db.one("SELECT payload_json FROM outbox WHERE dedup_key LIKE 'maxreply:%'")
    assert '-999' in row['payload_json']


@pytest.mark.asyncio
async def test_escalation_one_ping_and_ack_cancels_pending_ping(ctx, mx):
    iid = item(ctx)
    await ctx.alerts.max.process()
    for esc in (1,2):
        ctx.alerts.raise_alert(iid,'P1',escalation=esc)
        await ctx.alerts.max.process()
    assert ctx.db.scalar("SELECT count(*) FROM max_msgs WHERE kind='card'") == 1
    assert ctx.db.scalar("SELECT count(*) FROM max_msgs WHERE kind='ping'") == 1
    assert any(c[:2] == ('DELETE','/messages') for c in mx.calls)
    assert mx.sent()[-1][3]['link'] == {'type':'reply','mid':'mid.1'}
    ctx.alerts.raise_alert(iid,'P1',escalation=3)
    await handle_action(ctx, f'a:ack:{iid}', Actor('501','Иванов','manager','Max'), 'ack')
    await ctx.alerts.max.process()
    await ctx.alerts.flush_refresh()
    assert ctx.db.scalar("SELECT count(*) FROM max_msgs WHERE kind='ping'") == 0
    assert len(mx.sent()) == 3


@pytest.mark.asyncio
async def test_telegram_failure_immediate_fallback_even_unsubscribed(ctx, mx):
    ctx.settings.max.only_when_tg_down = True
    subscriptions.save(ctx,'max:-77',[])
    ctx.settings.alerts.telegram_fail_to_relay_after = 1
    iid = item(ctx)
    assert not ctx.db.one("SELECT 1 FROM outbox WHERE channel='max'")
    async def failed(*a): raise RuntimeError('offline')
    ctx.alerts.send_tg = failed
    await ctx.alerts.process()
    assert ctx.db.one("SELECT 1 FROM outbox WHERE channel='telegram' AND status='relay'")
    assert len(mx.sent()) == 1
    ctx.alerts.max.fallback(ctx.db.one("SELECT * FROM outbox WHERE channel='telegram' AND item_id=?", (iid,)))
    await ctx.alerts.max.process()
    assert len(mx.sent()) == 1


@pytest.mark.asyncio
async def test_max_is_not_blocked_by_slow_telegram(ctx, mx):
    iid = item(ctx)
    gate = asyncio.Event()
    async def slow(*a): await gate.wait();return {'message_id':20}
    ctx.alerts.send_tg = slow
    task = asyncio.create_task(ctx.alerts.process())
    for _ in range(20):
        if mx.sent(): break
        await asyncio.sleep(.01)
    assert mx.sent() and not task.done()
    gate.set(); await task


@pytest.mark.asyncio
async def test_rate_limit_and_transient_retry_remain_pending(ctx, mx):
    iid = item(ctx)
    mx.errors[('POST','/messages')] = [(429,'rate.limit',{'Retry-After':'23'})]
    await ctx.alerts.max.process()
    row = ctx.db.one("SELECT * FROM outbox WHERE channel='max'")
    assert row['status'] == 'pending' and row['attempts'] == 1
    from monitor.util import parse_iso
    assert 20 <= (parse_iso(row['next_attempt_at'])-now_utc()).total_seconds() <= 24
    ctx.alerts.ack(iid,'Operator','resolved')
    ctx.db.x("UPDATE outbox SET next_attempt_at=? WHERE channel='max'", (ago(minutes=1),))
    await ctx.alerts.max.process()
    assert ctx.db.scalar("SELECT status FROM outbox WHERE channel='max'") == 'dropped'


@pytest.mark.asyncio
@pytest.mark.parametrize('status,code',[(401,'access.denied'),(403,'chat.denied')])
async def test_token_or_membership_failure_pauses_and_notifies_other_channels(ctx, mx, status, code):
    ctx.db.x("UPDATE users SET email='admin@example.org'")
    item(ctx)
    mx.errors[('POST','/messages')] = [(status,code)]
    await ctx.alerts.max.process(); await ctx.alerts.max.process()
    assert len(mx.sent()) == 1
    assert ctx.db.scalar("SELECT status FROM outbox WHERE channel='max'") == 'paused'
    assert ctx.db.one("SELECT 1 FROM outbox WHERE channel='email' AND dedup_key LIKE 'maxfaultmail:%'")
    assert '🔴' in ctx.db.kv_get('max_health')['text']
    await ctx.max_bot.check_health()
    assert ctx.db.scalar("SELECT status FROM outbox WHERE channel='max'") == 'pending'


@pytest.mark.asyncio
async def test_old_card_edit_once_then_status_reply(ctx, mx):
    iid = item(ctx); await ctx.alerts.max.process()
    mx.errors[('PUT','/messages')] = [(400,'message.edit.timeout')]
    await handle_action(ctx,f'a:res:{iid}',Actor('501','Оператор','admin','Max'),'done')
    await ctx.alerts.flush_refresh(); await ctx.alerts.max.process()
    n = len([c for c in mx.calls if c[:2] == ('PUT','/messages')])
    await ctx.alerts.refresh_cards(iid)
    assert len([c for c in mx.calls if c[:2] == ('PUT','/messages')]) == n
    assert len(mx.sent()) == 2 and mx.sent()[-1][3]['link']['mid'] == 'mid.1'
    assert ctx.db.scalar("SELECT editable FROM max_msgs WHERE kind='card'") == 0


@pytest.mark.asyncio
async def test_deleted_original_status_reply_falls_back_to_plain_notice(ctx, mx):
    iid = item(ctx);await ctx.alerts.max.process()
    mx.errors[('PUT','/messages')] = [(404,'message.not.found')]
    await handle_action(ctx,f'a:res:{iid}',Actor('501','Оператор','admin','Max'),'done')
    await ctx.alerts.flush_refresh()
    mx.errors[('POST','/messages')] = [(404,'message.not.found')]
    await ctx.alerts.max.process()
    assert 'link' not in mx.sent()[-1][3]
    assert ctx.db.scalar("SELECT status FROM outbox WHERE dedup_key LIKE 'maxstatus:%'") == 'sent'


@pytest.mark.asyncio
async def test_night_p2_waits_silently_and_copy_does_not_get_card(ctx,mx,monkeypatch):
    monkeypatch.setattr(workhours,'is_off',lambda *a,**k:True)
    ctx.settings.alerts.night_hold = True
    iid = item(ctx)
    ctx.db.x("DELETE FROM outbox");ctx.db.x("UPDATE items SET priority='P2',analysis_json=?,match_json='{}',source_key='local' WHERE id=?", (jdump({'_risk':{'hold':True}}),iid))
    ctx.alerts.raise_alert(iid,'P2')
    await ctx.alerts.max.process()
    assert not mx.sent()
    row = ctx.db.one("SELECT * FROM outbox WHERE channel='max'")
    assert row['next_attempt_at'] > iso() and jload(row['payload_json'])['notify'] is False
    ctx.db.x("UPDATE items SET analysis_json=? WHERE id=?",(jdump({'_risk':{'copy_of':999}}),iid))
    ctx.db.x("UPDATE outbox SET next_attempt_at=?",(ago(minutes=1),))
    await ctx.alerts.max.process()
    assert not mx.sent() and ctx.db.scalar("SELECT status FROM outbox WHERE channel='max'") == 'dropped'


@pytest.mark.asyncio
async def test_foreign_house_incident_08_09_oct_zero_max_messages(ctx,mx,monkeypatch):
    monkeypatch.setattr(workhours,'is_off',lambda *a,**k:True)
    ctx.settings.alerts.night_hold=True
    for text,src,platform in [(NEWS47,'rss','rss'),(DTP_KUDROVO,'tg','tg'),(DTP_NOADDR,'tg','tg')]:
        post(ctx,text,src,platform)
    ctx.db.x("UPDATE alerts SET last_notified_at=?",(ago(hours=2),))
    assert ctx.alerts.escalate() == 0
    await ctx.alerts.max.process()
    assert not mx.sent()


@pytest.mark.asyncio
async def test_marker_saved_only_after_processed_and_callback_survives_restart(ctx,mx):
    iid=item(ctx);await ctx.alerts.max.process()
    mx.events=[callback(iid)]
    mx.errors[('POST','/answers')]=[(503,'service.unavailable')]
    assert not await ctx.max_bot.poll_once()
    assert ctx.db.kv_get('max_marker') is None
    assert ctx.db.item(iid)['workflow'] == 'in_work'
    ctx.max_bot=MaxBot(ctx)
    assert await ctx.max_bot.poll_once()
    assert ctx.db.kv_get('max_marker') == 987
    assert ctx.db.scalar("SELECT COUNT(*) FROM feedback WHERE verdict='in_work'") == 1


@pytest.mark.asyncio
async def test_old_callback_executes_but_old_scan_report_are_rejected(ctx,mx):
    iid=item(ctx)
    await ctx.max_bot.handle(callback(iid,timestamp=int((time.time()-3600)*1000)))
    assert ctx.db.item(iid)['workflow']=='in_work'
    for cmd in ('scan hot','report 24'):
        await ctx.max_bot.handle(message('/'+cmd,mid=cmd,age=700))
    replies=ctx.db.q("SELECT payload_json FROM outbox WHERE dedup_key LIKE 'maxreply:%'")
    assert len(replies)==2 and all('Команда устарела' in x['payload_json'] for x in replies)


def test_account_link_single_use_unique_expiring_and_deactivated(ctx,mx):
    actor=Actor('111','Оператор','admin','Telegram')
    code=link_code(ctx,actor)
    assert code not in ctx.db.scalar("SELECT digest FROM account_links")
    assert 'привязан' in redeem_link(ctx,code,'501')
    assert 'неверен' in redeem_link(ctx,code,'501')
    code=link_code(ctx,actor);ctx.db.x("UPDATE account_links SET expires_at=?",(ago(minutes=1),))
    assert 'истёк' in redeem_link(ctx,code,'501')
    code=link_code(ctx,actor);ctx.db.x("UPDATE users SET active=0")
    assert 'отключён' in redeem_link(ctx,code,'501')


@pytest.mark.asyncio
async def test_webhook_secret_durable_inbox_and_exact_public_path(ctx,mx,monkeypatch):
    monkeypatch.setenv('MAX_WEBHOOK_SECRET','a-secure-test-secret-123')
    ctx.settings.max.mode='webhook'
    app=FastAPI();app.include_router(make_router(ctx));app.add_middleware(LocalOnly)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,client=('8.8.8.8',1)),base_url='https://radar.test') as client:
        assert (await client.post('/api/max/webhook',json=message('/status'))).status_code==403
        headers={'X-Max-Bot-Api-Secret':'a-secure-test-secret-123'}
        event=message('/status')
        assert (await client.post('/api/max/webhook',json=event,headers=headers)).status_code==200
        assert (await client.post('/api/max/webhook',json=event,headers=headers)).status_code==200
        assert ctx.db.scalar('SELECT count(*) FROM max_events')==1
        assert (await client.post('/api/max/webhook/extra',json=event,headers=headers)).status_code==403
        assert (await client.get('/settings')).status_code==403
        assert (await client.post('/api/max/webhook',content='x'*256001,headers=headers)).status_code==413
    await ctx.max_bot.drain()
    assert ctx.db.scalar('SELECT status FROM max_events')=='done'


@pytest.mark.asyncio
async def test_transport_switch_and_webhook_resubscribe(ctx,mx,monkeypatch):
    mx.subs=[{'url':'https://old.example/api/max/webhook'}]
    await ctx.max_bot.configure_transport()
    assert not mx.subs
    monkeypatch.setenv('MAX_WEBHOOK_SECRET','a-secure-test-secret-123')
    ctx.settings.max.mode='webhook';ctx.settings.public_url='https://radar.example'
    await ctx.max_bot.configure_transport()
    assert len(mx.subs)==1 and mx.subs[0]['secret']=='a-secure-test-secret-123'
    mx.subs=[];await ctx.max_bot.configure_transport()
    assert len(mx.subs)==1


@pytest.mark.asyncio
async def test_permission_loss_and_bot_added_unknown_group(ctx,mx):
    mx.permissions=['write']
    await ctx.max_bot.check_health()
    assert 'команды в группе — нет' in ctx.db.kv_get('max_health')['text']
    event={'update_type':'bot_added','timestamp':int(time.time()*1000),'chat_id':-999,'user':{'user_id':501,'is_bot':False},'is_channel':False}
    await ctx.max_bot.handle(event)
    assert ctx.db.kv_get('max_seen_chat:-999')['chat_id']=='-999'
    assert ctx.settings.max_admin_chat_id=='-77'
    assert ctx.db.one("SELECT 1 FROM outbox WHERE dedup_key LIKE 'adm:max-added:%'")


@pytest.mark.asyncio
async def test_files_uploaded_once_attachment_not_ready_retries(ctx,mx):
    from monitor.settings import DATA_DIR
    path=DATA_DIR/'reports'/'test-max.docx';path.write_bytes(b'fixture file')
    ctx.alerts.max.enqueue_text('-77',Card('Отчёт'),'report-test',files=[path])
    mx.errors[('POST','/messages')]=[(400,'attachment.not.ready')]
    await ctx.alerts.max.process()
    assert ctx.db.scalar("SELECT status FROM outbox WHERE dedup_key='report-test'")=='pending'
    ctx.db.x("UPDATE outbox SET next_attempt_at=?",(ago(minutes=1),))
    await ctx.alerts.max.process()
    assert ctx.db.scalar("SELECT status FROM outbox WHERE dedup_key='report-test'")=='sent'
    assert len([c for c in mx.calls if c[1]=='/uploads'])==1
    upload=[c for c in mx.calls if c[1]=='/file'][0]
    assert 'authorization' not in upload[4]


@pytest.mark.asyncio
async def test_pin_open_p1_unpins_on_ack(ctx,mx):
    ctx.settings.max.pin_p1=True
    iid=item(ctx);await ctx.alerts.max.process()
    assert mx.pinned=='mid.1'
    await handle_action(ctx,f'a:ack:{iid}',Actor('501','Op','manager','Max'),'pin-ack')
    await ctx.alerts.flush_refresh()
    assert mx.pinned is None


@pytest.mark.asyncio
async def test_private_admin_commands_are_forbidden_in_group_and_for_manager(ctx,mx):
    for role,private in [('admin',False),('manager',True),('viewer',True)]:
        cards=await command(ctx,'set','max.pin_p1 true',Actor('501','Op',role,'Max'),private=private)
        assert 'администратору' in cards[0].text_html
        assert not ctx.settings.max.pin_p1
    cards=await command(ctx,'set','max.pin_p1 true',Actor('501','Op','admin','Max'),private=True)
    assert ctx.settings.max.pin_p1


def test_migration_preserves_existing_users_and_is_repeatable(tmp_path):
    path=tmp_path/'legacy.sqlite3'
    c=sqlite3.connect(path);c.execute('CREATE TABLE users(tg_id TEXT PRIMARY KEY,name TEXT,role TEXT,email TEXT,level INTEGER,active INTEGER,added_at TEXT)')
    c.execute("INSERT INTO users(tg_id,name,role) VALUES('123','Имя','manager')");c.commit();c.close()
    for _ in range(2):
        db=DB(path)
        assert db.one("SELECT * FROM users WHERE tg_id='123'")['name']=='Имя'
        assert db.one("SELECT * FROM users WHERE tg_id='123'")['max_user_id'] is None
        db.conn.close()


@pytest.mark.parametrize('state',['open','in_work','resolved','false_positive','dropped','escalated'])
def test_telegram_rendering_matches_baseline_byte_for_byte(ctx,state):
    ns={'__package__':'monitor.alerts'}
    exec((Path(__file__).parent/'fixtures'/'telegram_formatting_v20261009.txt').read_text(),ns)
    iid=item(ctx);it=ctx.db.item(iid)
    kwargs={'state':state,'who':'Иванов','when':'13.10 14:05','note':'проверка','foreign':{'label':'Европейский 13к1'},'check':True}
    assert formatting.tg_card(it,**kwargs)==ns['tg_card'](it,**kwargs)
    assert formatting.tg_alert(it)==ns['tg_alert'](it)


def test_max_html_bounded_valid_and_button_rows():
    text='<b>'+'🟠 &amp; &lt;'*1500+'</b><script>bad</script><a href="javascript:evil">bad link</a>'
    body=Card(text,[[Btn(str(i),f'a:ack:{i}') for i in range(8)]]).max_body()
    assert len(body['text'])<=4000
    etree.fromstring(('<root>'+body['text']+'</root>').encode())
    assert '<script>' not in body['text'] and 'javascript:' not in body['text']
    assert [len(x) for x in body['attachments'][0]['payload']['buttons']]==[3,3,2]


def test_subscriptions_max_defaults_and_custom_p2_off(ctx,mx):
    rows=subscriptions.rows(ctx)
    group=next(x for x in rows if x['id']=='max:-77')
    assert {'p1','p2','esc','digest','story','service','rep_morning','rep_weekly'}==set(group['events'])
    assert next(x for x in rows if x['id']=='max:user:501')['events']==[]
    subscriptions.save(ctx,'max:-77',['p1'])
    assert subscriptions.recipients(ctx,'p2','max')==[]
    assert any(x['max'] and x['name']=='Админ-группа Max' for x in subscriptions.people(ctx))

@pytest.mark.asyncio
async def test_nonpublic_company_draft_does_not_advertise_affiliation(ctx,mx):
    iid=post(ctx,'ЖК Лампо. УК Возрождение Северо-Запада не ремонтирует подъезд. Графская 6к1.')
    it=ctx.db.item(iid);a=jload(it['analysis_json'],{})
    a['reply_draft']='Мы из ГК Балтийский Дом, наша УК Возрождение Северо-Запада уже выехала.'
    a['summary']='Компания входит в ГК Балтийский Дом.'
    ctx.db.x('UPDATE items SET analysis_json=? WHERE id=?',(jdump(a),iid))
    reply=await handle_action(ctx,f'a:rep:{iid}',Actor('501','Op','viewer','Max'))
    assert 'Балтийский Дом' not in reply.reply_html and 'Текст ИИ скрыт' in reply.reply_html
    card=card_for(ctx,iid)
    assert 'Балтийский Дом' not in card.text_html


@pytest.mark.asyncio
async def test_ack_during_ping_send_removes_late_ping(ctx,mx):
    iid=item(ctx);await ctx.alerts.max.process()
    ctx.alerts.raise_alert(iid,'P1',escalation=1)
    original=ctx.alerts.max.api.send
    async def race(body,**target):
        if body.get('link'):
            await handle_action(ctx,f'a:ack:{iid}',Actor('501','Op','manager','Max'),'race')
        return await original(body,**target)
    ctx.alerts.max.api.send=race
    await ctx.alerts.max.process()
    assert ctx.db.scalar("SELECT count(*) FROM max_msgs WHERE kind='ping'")==0
    assert ctx.db.scalar("SELECT status FROM outbox WHERE dedup_key LIKE 'maxping:%'")=='sent'


@pytest.mark.asyncio
async def test_refresh_failure_is_durable_and_retries_latest_state(ctx,mx):
    iid=item(ctx);await ctx.alerts.max.process()
    mx.errors[('PUT','/messages')]=[(503,'service.unavailable')]
    await handle_action(ctx,f'a:ack:{iid}',Actor('501','Op','manager','Max'),'r1')
    await ctx.alerts.flush_refresh()
    assert ctx.db.one('SELECT 1 FROM card_refresh WHERE item_id=?',(iid,))
    await handle_action(ctx,f'a:res:{iid}',Actor('501','Op2','manager','Telegram'),'r2')
    await ctx.alerts.flush_refresh()
    assert not ctx.db.one('SELECT 1 FROM card_refresh WHERE item_id=?',(iid,))
    assert 'Решено' in [c for c in mx.calls if c[:2]==('PUT','/messages')][-1][3]['text']


@pytest.mark.asyncio
async def test_manual_escalation_has_authoritative_state(ctx,mx):
    iid=item(ctx);await ctx.alerts.max.process()
    await handle_action(ctx,f'a:esc:{iid}',Actor('501','Op','manager','Max'),'manual-esc')
    await ctx.alerts.flush_refresh()
    assert 'Эскалировано' in [c for c in mx.calls if c[:2]==('PUT','/messages')][-1][3]['text']


@pytest.mark.asyncio
async def test_manual_show_card_is_deduplicated(ctx,mx):
    iid=item(ctx)
    await ctx.max_bot.show_card(iid,'-77')
    await ctx.max_bot.show_card(iid,'-77')
    await ctx.alerts.max.process()
    assert len(mx.sent())==1


@pytest.mark.asyncio
async def test_no_max_token_no_network_no_max_rows(ctx,monkeypatch):
    monkeypatch.delenv('MAX_BOT_TOKEN',raising=False)
    monkeypatch.setenv('MAX_ADMIN_CHAT_ID','-77')
    item(ctx)
    assert not ctx.db.one("SELECT 1 FROM outbox WHERE channel='max'")
    assert await ctx.alerts.max.process()==0
    assert ctx.alerts.max._api is None

@pytest.mark.asyncio
async def test_queued_private_report_dropped_after_user_unlinked(ctx,mx):
    ctx.alerts.max.enqueue_text('user:501',Card('private report'),'private-report')
    ctx.db.x("UPDATE users SET max_user_id=NULL")
    await ctx.alerts.max.process()
    assert not mx.sent()
    assert ctx.db.scalar("SELECT status FROM outbox WHERE dedup_key='private-report'")=='dropped'


@pytest.mark.asyncio
async def test_pending_card_to_previous_admin_group_dropped(ctx,mx,monkeypatch):
    item(ctx)
    monkeypatch.setenv('MAX_ADMIN_CHAT_ID','-888')
    await ctx.alerts.max.process()
    assert not mx.sent()
    assert ctx.db.scalar("SELECT status FROM outbox WHERE channel='max'")=='dropped'
