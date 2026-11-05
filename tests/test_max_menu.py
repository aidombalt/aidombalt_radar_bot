"""MAX menu journeys through the real event handler, API bodies and persistent settings."""
import asyncio
import time
from types import SimpleNamespace

import pytest

from monitor.alerts import commands, subscriptions
from monitor.alerts.max_bot import COMMANDS, MaxBot
from monitor.util import iso, jdump, jload
from tests.test_max import mx, callback, item, message  # shared offline transport fixture


def buttons(body):
    return [b for a in body.get('attachments', []) if a['type'] == 'inline_keyboard'
            for row in a['payload']['buttons'] for b in row]


def select(body, action):
    return next(b['payload'] for b in buttons(body) if b.get('payload') == action
                or b.get('payload', '').split(':', 2)[-1] == action)


async def open_menu(ctx, mx, role='admin', private=True, mid='menu', command='/menu'):
    ctx.db.x("UPDATE users SET role=? WHERE max_user_id='501'", (role,))
    await ctx.max_bot.handle(message(command, private=private, chat=501 if private else -77, mid=mid))
    await ctx.alerts.max.process()
    return mx.sent()[-1][3]


async def click(ctx, mx, body, action, cid='menu-cb', private=True, uid=501, timestamp=None):
    event = callback(1, cid=cid, uid=uid, chat=uid if private else -77)
    event['message']['recipient']['chat_type'] = 'dialog' if private else 'chat'
    event['callback']['payload'] = select(body, action) if body else action
    if timestamp is not None:
        event['callback']['timestamp'] = timestamp
    await ctx.max_bot.handle(event)
    answer = next(c for c in reversed(mx.calls) if c[:2] == ('POST', '/answers'))
    return answer[3].get('message'), answer[3]['notification']


@pytest.mark.asyncio
@pytest.mark.parametrize('command', ['/menu', '/start'])
async def test_menu_and_start_show_real_inline_navigation(ctx, mx, command):
    body = await open_menu(ctx, mx, command=command)
    assert 'Меню Радара' in body['text']
    assert select(body, 'report') and select(body, 'settings')
    assert all(len(row) <= 3 for a in body['attachments'] for row in a['payload']['buttons'])
    assert len(body['text']) <= 4000
    assert 'menu' in {x['name'] for x in COMMANDS}


@pytest.mark.asyncio
@pytest.mark.parametrize('role,private,reports,settings', [
    ('viewer', True, False, False), ('manager', True, True, False),
    ('admin', True, True, True), ('admin', False, True, False),
])
async def test_menu_exposes_only_current_role_actions(ctx, mx, role, private, reports, settings):
    body = await open_menu(ctx, mx, role=role, private=private)
    actions = {b.get('payload', '').split(':', 2)[-1] for b in buttons(body)}
    assert ('report' in actions) == reports
    assert ('settings' in actions) == settings


@pytest.mark.asyncio
async def test_status_and_back_edit_same_message_without_new_posts(ctx, mx):
    body = await open_menu(ctx, mx)
    n = len(mx.sent())
    status, _ = await click(ctx, mx, body, 'status')
    assert 'Радар — состояние' in status['text']
    home, _ = await click(ctx, mx, status, 'home', cid='back')
    assert 'Меню Радара' in home['text']
    assert len(mx.sent()) == n


@pytest.mark.asyncio
async def test_subscriptions_and_file_switch_share_web_settings_and_are_idempotent(ctx, mx):
    menu = await open_menu(ctx, mx)
    settings, _ = await click(ctx, mx, menu, 'settings')
    settings, notice = await click(ctx, mx, settings, 'sub:rep_day:1', cid='sub')
    assert 'сохранены' in notice
    assert '-77' in subscriptions.recipients(ctx, 'rep_day', 'max')
    session = select(settings, 'sub:rep_day:0').split(':')[1]
    # Retried callback ID cannot reverse or repeat an action.
    await click(ctx, mx, None, f'm:{session}:sub:rep_day:1', cid='sub')
    assert '-77' in subscriptions.recipients(ctx, 'rep_day', 'max')
    options, _ = await click(ctx, mx, settings, 'options', cid='options')
    await click(ctx, mx, options, 'option:report_files:1', cid='files')
    assert ctx.settings.max.report_files
    assert ctx.db.kv_get('settings_overrides')['max']['report_files'] is True


@pytest.mark.asyncio
@pytest.mark.parametrize('private,role', [(False, 'admin'), (True, 'manager'), (True, 'viewer')])
async def test_forged_admin_button_does_not_change_settings(ctx, mx, private, role):
    body = await open_menu(ctx, mx, role=role, private=private)
    token = buttons(body)[0]['payload'].split(':')[1]
    _, notice = await click(ctx, mx, None, f'm:{token}:option:report_files:1', private=private)
    assert 'администратору в личке' in notice
    assert not ctx.settings.max.report_files


@pytest.mark.asyncio
async def test_menu_owner_chat_expiry_and_revocation(ctx, mx):
    body = await open_menu(ctx, mx)
    ctx.db.x("INSERT INTO users(tg_id,max_user_id,name,role,active) VALUES('222','502','Другой','admin',1)")
    _, notice = await click(ctx, mx, body, 'status', uid=502)
    assert 'своё меню' in notice
    _, notice = await click(ctx, mx, body, 'status', private=False, cid='wrong-chat')
    assert 'своё меню' in notice
    token = buttons(body)[0]['payload'].split(':')[1]
    session = ctx.max_bot.menu.session(token)
    session['expires'] = time.time()-1
    ctx.max_bot.menu.save(token, session)
    _, notice = await click(ctx, mx, body, 'status', cid='expired')
    assert 'устарело' in notice
    ctx.db.x("UPDATE users SET max_user_id=NULL WHERE tg_id='111'")
    _, notice = await click(ctx, mx, body, 'status', cid='revoked')
    assert 'Нет доступа' in notice


@pytest.mark.asyncio
async def test_stale_settings_button_rejected_and_navigation_remains_available(ctx, mx):
    menu = await open_menu(ctx, mx)
    settings, _ = await click(ctx, mx, menu, 'settings')
    _, notice = await click(ctx, mx, settings, 'sub:rep_day:1', cid='old', timestamp=int((time.time()-700)*1000))
    assert 'устарело' in notice
    assert not subscriptions.recipients(ctx, 'rep_day', 'max')
    status, _ = await click(ctx, mx, menu, 'status', cid='read-old', timestamp=int((time.time()-700)*1000))
    assert 'Радар — состояние' in status['text']


@pytest.mark.asyncio
async def test_scan_runs_once_and_viewer_cannot_forge_launch(ctx, mx):
    launches = []
    ctx.max_bot.service = SimpleNamespace(run_now=lambda tier: launches.append(tier) or True)
    menu = await open_menu(ctx, mx)
    scan, _ = await click(ctx, mx, menu, 'scan')
    await click(ctx, mx, scan, 'scan:hot', cid='launch')
    await click(ctx, mx, scan, 'scan:hot', cid='launch')
    assert launches == ['hot']
    ctx.db.x("UPDATE users SET role='viewer' WHERE max_user_id='501'")
    _, notice = await click(ctx, mx, scan, 'scan:hot', cid='viewer-launch')
    assert 'менеджеру' in notice
    assert launches == ['hot']


@pytest.mark.asyncio
async def test_source_detail_and_toggle_use_same_source_registry(ctx, mx):
    menu = await open_menu(ctx, mx)
    sources, _ = await click(ctx, mx, menu, 'sources:0')
    action = next(b['payload'].split(':',2)[-1] for b in buttons(sources) if ':source:' in b.get('payload',''))
    ref = action.split(':')[1]
    token = buttons(sources)[0]['payload'].split(':')[1]
    key = ctx.max_bot.menu.session(token)['refs'][ref]
    detail, _ = await click(ctx, mx, sources, action, cid='detail')
    assert key in detail['text']
    await click(ctx, mx, detail, f'source_set:{ref}:0', cid='off')
    assert ctx.db.scalar('SELECT enabled FROM sources WHERE key=?', (key,)) == 0


@pytest.mark.asyncio
async def test_candidate_approval_is_private_role_checked_and_persistent(ctx, mx):
    ctx.db.upsert_source({'key':'vk_wall:-999','kind':'vk_wall','handle':'-999','title':'Новый паблик',
                         'candidate':1,'enabled':0,'members':2000,'meta':{'owner_id':-999,'ev':[],'n':1}})
    menu = await open_menu(ctx, mx, role='manager')
    candidates, _ = await click(ctx, mx, menu, 'cand:0')
    action = next(b['payload'].split(':',2)[-1] for b in buttons(candidates) if ':approve:' in b.get('payload',''))
    await click(ctx, mx, candidates, action, cid='approve')
    await click(ctx, mx, candidates, action, cid='approve')
    source = ctx.db.one("SELECT * FROM sources WHERE key='vk_wall:-999'")
    assert source['candidate'] == 0 and source['enabled'] == 1


@pytest.mark.asyncio
async def test_search_prompt_scoped_input_and_card_open_from_results(ctx, mx):
    iid = item(ctx)
    menu = await open_menu(ctx, mx)
    prompt, _ = await click(ctx, mx, menu, 'item')
    assert 'номер карточки' in prompt['text']
    await ctx.max_bot.handle(message(str(iid), private=True, chat=501, mid='typed-id'))
    await ctx.alerts.max.process()
    assert ctx.db.one("SELECT 1 FROM max_msgs WHERE item_id=? AND chat_id='user:501'", (iid,))
    card = next(c[3] for c in mx.sent() if any(b.get('payload')=='menu:open' for b in buttons(c[3])))
    assert select(card, 'menu:open') == 'menu:open'
    await click(ctx, mx, None, 'menu:open', cid='card-menu')
    await ctx.alerts.max.process()
    assert 'Меню Радара' in mx.sent()[-1][3]['text']


@pytest.mark.asyncio
async def test_background_report_does_not_block_menu_and_duplicate_request(ctx, mx, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    builds = []

    async def build(*args):
        builds.append(args[1]); started.set()
        await release.wait()
        return {'short':'Отчёт готов','xlsx':'fake.xlsx','docx':'fake.docx'}

    monkeypatch.setattr(commands, 'build_report', build)
    menu = await open_menu(ctx, mx)
    periods, _ = await click(ctx, mx, menu, 'report')
    try:
        waiting, _ = await click(ctx, mx, periods, 'report:24', cid='report')
        await asyncio.wait_for(started.wait(), 2)
        assert 'меню можно продолжать' in waiting['text']
        await click(ctx, mx, periods, 'report:24', cid='report')
        status, _ = await click(ctx, mx, menu, 'status', cid='while-building')
        assert 'Радар — состояние' in status['text']
        release.set()
        await asyncio.wait_for(ctx.max_bot.menu._worker, 2)
        await ctx.alerts.max.process()
        assert builds == [24]
        assert any('Отчёт готов' in c[3]['text'] for c in mx.sent())
        assert ctx.db.scalar("SELECT count(*) FROM outbox WHERE dedup_key LIKE 'max_report_job:%:result'") == 1
    finally:
        release.set()
        await ctx.max_bot.menu.close()


@pytest.mark.asyncio
async def test_report_request_survives_restart_and_revoked_owner_is_cancelled(ctx, mx, monkeypatch):
    builds=[]
    async def build(*args):
        builds.append(args[1])
        return {'short':'Отчёт после перезапуска','xlsx':'fake.xlsx','docx':'fake.docx'}
    monkeypatch.setattr(commands, 'build_report', build)
    job={'owner':'501','destination':'user:501','private':True,'hours':12,'token':'old-menu',
         'created':iso(),'status':'running'}
    ctx.db.kv_set('max_report_job:restart', job)
    ctx.max_bot=MaxBot(ctx)
    ctx.max_bot.menu.kick_reports()
    await ctx.max_bot.menu._worker
    await ctx.alerts.max.process()
    assert builds == [12]
    assert ctx.db.kv_get('max_report_job:restart')['status'] == 'done'
    ctx.db.kv_set('max_report_job:revoked', job)
    ctx.db.x("UPDATE users SET max_user_id=NULL")
    ctx.max_bot.menu.kick_reports()
    await ctx.max_bot.menu._worker
    assert ctx.db.kv_get('max_report_job:revoked')['status'] == 'cancelled'
    assert builds == [12]


@pytest.mark.asyncio
async def test_callback_edit_failure_falls_back_without_repeating_mutation(ctx, mx):
    menu = await open_menu(ctx, mx)
    settings, _ = await click(ctx, mx, menu, 'settings')
    mx.errors[('POST','/answers')] = [(400,'callback.expired')]
    await click(ctx, mx, settings, 'sub:rep_day:1', cid='expired-edit')
    await ctx.alerts.max.process()
    assert subscriptions.recipients(ctx, 'rep_day', 'max') == ['-77']
    assert any('Что присылать в группу Max' in c[3]['text'] for c in mx.sent())


@pytest.mark.asyncio
async def test_search_input_navigation_and_cancellation(ctx, mx):
    iid = item(ctx)
    menu = await open_menu(ctx, mx)
    prompt, _ = await click(ctx, mx, menu, 'find')
    assert 'поиска' in prompt['text']
    await ctx.max_bot.handle(message('лифт', private=True, chat=501, mid='search-query'))
    await ctx.alerts.max.process()
    search = mx.sent()[-1][3]
    assert 'Результаты поиска' in search['text'] and select(search, f'item:{iid}')
    # Going back cancels a pending free-text operation.
    prompt, _ = await click(ctx, mx, menu, 'find', cid='search-again')
    await click(ctx, mx, prompt, 'home', cid='cancel-search')
    n = len(ctx.db.q('SELECT * FROM outbox'))
    await ctx.max_bot.handle(message('обычное сообщение', private=True, chat=501, mid='not-search'))
    assert len(ctx.db.q('SELECT * FROM outbox')) == n


@pytest.mark.asyncio
async def test_watch_button_input_and_removal_preserve_user_config(ctx, mx, monkeypatch, tmp_path):
    import monitor.context as context
    import monitor.settings as settings
    import yaml

    cfg = tmp_path/'config'; cfg.mkdir()
    path = cfg/'watchlist.yaml'
    path.write_text('people: []\nkeywords: [existing]\n', encoding='utf-8')
    monkeypatch.setattr(settings, 'CONFIG_DIR', cfg)
    monkeypatch.setattr(context, 'CONFIG_DIR', cfg)
    ctx.reload_registry(recover=False)
    menu = await open_menu(ctx, mx)
    watch, _ = await click(ctx, mx, menu, 'watch')
    await click(ctx, mx, watch, 'watch_add', cid='watch-add')
    await ctx.max_bot.handle(message('Иван Тестовый', private=True, chat=501, mid='typed-name'))
    assert ctx.watch.people[0]['name'] == 'Иван Тестовый'
    watch, _ = await click(ctx, mx, menu, 'watch', cid='watch-list')
    action = next(b['payload'].split(':',2)[-1] for b in buttons(watch) if ':unwatch:' in b.get('payload',''))
    await click(ctx, mx, watch, action, cid='remove-watch')
    data = yaml.safe_load(path.read_text('utf-8'))
    assert data['people'] == [] and data['keywords'] == ['existing']


@pytest.mark.asyncio
async def test_report_shutdown_restores_queued_job(ctx, mx, monkeypatch):
    started = asyncio.Event()
    async def build(*args):
        started.set()
        await asyncio.Future()
    monkeypatch.setattr(commands, 'build_report', build)
    ctx.db.kv_set('max_report_job:shutdown', {'owner':'501','destination':'user:501','private':True,
                                           'hours':24,'token':'old','created':iso(),'status':'queued'})
    ctx.max_bot.menu.kick_reports()
    await asyncio.wait_for(started.wait(), 2)
    await ctx.max_bot.menu.close()
    assert ctx.db.kv_get('max_report_job:shutdown')['status'] == 'queued'
