"""Telegram journeys exercise the real dispatcher and ensure edits replace chat spam."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import AnswerCallbackQuery, EditMessageReplyMarkup, EditMessageText, SendDocument, SendMessage
from aiogram.types import CallbackQuery, Chat, InaccessibleMessage, Message, Update, User

from monitor.alerts import commands
from monitor.alerts.bot import MENU, build_bot
from monitor.alerts.telegram_menu import TelegramMenu
from tests.test_bot import MockSession
from tests.test_foreign_house import post

NOW = datetime.now(timezone.utc)


class Session(MockSession):
    def __init__(self):
        super().__init__()
        self.messages = {}
        self.n = 100
        self.edit_error = None

    async def make_request(self, bot, method, timeout=None):
        self.sent.append(method)
        if isinstance(method, EditMessageText) and self.edit_error:
            error = self.edit_error; self.edit_error = None
            if error == 'network':
                raise TelegramNetworkError(method=method, message='offline')
            raise TelegramBadRequest(method=method, message=error)
        if isinstance(method, (AnswerCallbackQuery, EditMessageReplyMarkup)):
            return True
        if isinstance(method, SendMessage):
            self.n += 1; mid = self.n
        else:
            mid = getattr(method, 'message_id', None) or 200
        chat_id = int(method.chat_id)
        message = Message(message_id=mid, date=NOW, chat=Chat(id=chat_id, type='private' if chat_id>0 else 'supergroup'),
                          from_user=User(id=bot.id, is_bot=True, first_name='Радар'), text=getattr(method,'text',''),
                          reply_markup=getattr(method,'reply_markup', None)).as_(bot)
        if isinstance(method, (SendMessage, EditMessageText)):
            self.messages[(chat_id,mid)] = message
        return message


@pytest.fixture
def tg(ctx, monkeypatch):
    monkeypatch.delenv('TG_ADMINS', raising=False)
    monkeypatch.delenv('RELAY_URL', raising=False)
    session = Session()
    launches=[]
    service=SimpleNamespace(run_now=lambda tier: launches.append(tier) or True)
    bot, dp = build_bot(ctx, service, session=session, token='123456:TEST-TOKEN-abcdefghijklmnopqrstuvwxyz')
    return bot, dp, session, launches


async def command(tg, text, uid=111, chat_id=None):
    bot, dp, _, _ = tg
    chat_id=chat_id or uid
    m=Message(message_id=1, date=NOW, chat=Chat(id=chat_id,type='private' if chat_id>0 else 'supergroup'),
              from_user=User(id=uid,is_bot=False,first_name='Оператор'),text=text)
    await dp.feed_update(bot,Update(update_id=1,message=m))


async def press(tg, data, mid=101, uid=111, chat_id=None, cid=None, inaccessible=False):
    bot, dp, session, _ = tg
    chat_id=chat_id or uid
    m=session.messages.get((chat_id,mid)) or Message(message_id=mid,date=NOW,
                chat=Chat(id=chat_id,type='private' if chat_id>0 else 'supergroup'),text='card')
    if inaccessible:
        m=InaccessibleMessage(chat=m.chat,message_id=mid,date=0)
    cb=CallbackQuery(id=cid or 'cb'+str(len(session.sent)), from_user=User(id=uid,is_bot=False,first_name='Оператор'),
                     chat_instance='test',message=m,data=data)
    await dp.feed_update(bot,Update(update_id=2,callback_query=cb))


def sent(session):
    return [m for m in session.sent if isinstance(m,SendMessage)]


def edits(session):
    return [m for m in session.sent if isinstance(m,EditMessageText)]


def payloads(message):
    return [b.callback_data for row in message.reply_markup.inline_keyboard for b in row]


@pytest.mark.asyncio
async def test_menu_status_brain_help_back_use_one_message(ctx,tg):
    await command(tg,'/menu')
    for data in ('m:status','m:home','m:brain','m:home','m:help','m:home','m:watch','m:home','m:sources'):
        await press(tg,data)
    session=tg[2]
    assert len(sent(session))==1 and len(edits(session))==9
    assert {m.message_id for m in edits(session)}=={101}
    assert 'm:home' in payloads(session.messages[(111,101)])


@pytest.mark.asyncio
async def test_typed_read_commands_reuse_current_panel(ctx,tg):
    await command(tg,'/menu')
    for text in ('/status','/help','/brain','/feed','/sources'):
        await command(tg,text)
    assert len(sent(tg[2]))==1
    assert len(edits(tg[2]))==5
    await command(tg,'/menu')
    assert len(sent(tg[2]))==2  # explicit reopen makes the panel visible below later alerts


@pytest.mark.asyncio
async def test_feed_pages_card_details_and_back_do_not_create_or_register_alerts(ctx,tg):
    for n in range(7):
        post(ctx,f'ЖК Виктория, Авиаторов Балтики 9к1: лифт не работает уже {n+2} дней. Александр Юрьевич, помогите!')
    await command(tg,'/menu')
    await press(tg,'m:feed')
    assert 'm:page:1' in payloads(tg[2].messages[(111,101)])
    await press(tg,'m:page:1')
    text=tg[2].messages[(111,101)].text
    chosen=next(x for x in payloads(tg[2].messages[(111,101)]) if x.startswith('m:item:'))
    iid=int(chosen.rsplit(':',1)[1])
    await press(tg,chosen)
    await press(tg,f'a:rep:{iid}')
    assert 'Черновик' in tg[2].messages[(111,101)].text
    await press(tg,f'm:item:{iid}')
    await press(tg,'m:parent')
    assert tg[2].messages[(111,101)].text==text
    assert len(sent(tg[2]))==1
    assert not ctx.db.one("SELECT 1 FROM alert_msgs WHERE chat_id='111' AND message_id=101")


@pytest.mark.asyncio
async def test_original_alert_draft_and_back_edit_original_message(ctx,tg):
    iid=post(ctx,'ЖК Виктория: лифт стоит второй день, УК молчит. Александр Юрьевич, помогите!')
    await press(tg,f'a:rep:{iid}',mid=5)
    assert 'Черновик' in edits(tg[2])[-1].text
    await press(tg,f'a:card:{iid}',mid=5)
    assert 'Черновик' not in edits(tg[2])[-1].text
    assert not sent(tg[2])


@pytest.mark.asyncio
async def test_viewer_menu_hides_scan_and_report_and_forged_button_is_denied(ctx,tg):
    ctx.db.x("UPDATE users SET role='viewer' WHERE tg_id='111'")
    await command(tg,'/menu')
    assert 'm:scan' not in payloads(tg[2].messages[(111,101)])
    await press(tg,'m:scan:hot')
    assert not tg[3] and len(sent(tg[2]))==1
    assert 'Только менеджер' in [m for m in tg[2].sent if isinstance(m,AnswerCallbackQuery)][-1].text


@pytest.mark.asyncio
async def test_scan_retry_runs_once_in_same_panel(ctx,tg):
    await command(tg,'/menu')
    await press(tg,'m:scan')
    await press(tg,'m:scan:hot',cid='same-scan')
    await press(tg,'m:scan:hot',cid='same-scan')
    assert tg[3]==['hot']
    assert len(sent(tg[2]))==1


@pytest.mark.asyncio
async def test_group_panel_owner_checked_before_actions(ctx,tg):
    ctx.db.x("INSERT INTO users(tg_id,name,role,active) VALUES('222','Другой','admin',1)")
    await command(tg,'/menu',chat_id=-77)
    await press(tg,'m:scan:hot',uid=222,chat_id=-77)
    assert not tg[3] and not edits(tg[2])


@pytest.mark.asyncio
@pytest.mark.parametrize('error', ["message can't be edited",'message to edit not found','message is not modified'])
async def test_edit_failure_replaces_only_missing_panel(ctx,tg,error):
    await command(tg,'/menu')
    tg[2].edit_error=error
    await press(tg,'m:status')
    assert len(sent(tg[2]))==(1 if error=='message is not modified' else 2)
    await command(tg,'/brain')
    assert len(sent(tg[2]))==(1 if error=='message is not modified' else 2)


@pytest.mark.asyncio
async def test_inaccessible_message_uses_edit_api_without_shortcut_crash(ctx,tg):
    await command(tg,'/menu')
    await press(tg,'m:status',inaccessible=True)
    assert len(sent(tg[2]))==1
    assert 'Радар — состояние' in edits(tg[2])[-1].text


@pytest.mark.asyncio
async def test_network_error_does_not_create_duplicate_panel(ctx,tg):
    panel=TelegramMenu(ctx,tg[0],MENU)
    tg[2].edit_error='network'
    from monitor.alerts.render import Card
    with pytest.raises(TelegramNetworkError):
        await panel.edit_raw(111,101,Card('Статус'))
    assert not sent(tg[2])


def report_result():
    return {'id':7,'short':'<b>Отчёт готов</b>','xlsx':'/tmp/test.xlsx','docx':'/tmp/test.docx',
            'items':2,'narrative_by':'шаблон','subject':'Радар','html':'<p>Отчёт</p>'}


@pytest.mark.asyncio
async def test_report_updates_panel_sends_only_documents_and_duplicate_is_ignored(ctx,tg,monkeypatch):
    calls=[]
    async def build(*args):
        calls.append(args[1]); return report_result()
    monkeypatch.setattr(commands,'build_report',build)
    await command(tg,'/menu')
    await press(tg,'m:report')
    await press(tg,'m:report:24',cid='same-report')
    await press(tg,'m:report:24',cid='same-report')
    assert calls==[24]
    assert len(sent(tg[2]))==1
    assert len([m for m in tg[2].sent if isinstance(m,SendDocument)])==2
    assert 'Отчёт готов' in edits(tg[2])[-1].text


@pytest.mark.asyncio
async def test_report_completion_does_not_replace_newer_navigation(ctx,tg,monkeypatch):
    started,release=asyncio.Event(),asyncio.Event()
    async def build(*args):
        started.set(); await release.wait(); return report_result()
    monkeypatch.setattr(commands,'build_report',build)
    await command(tg,'/menu')
    task=asyncio.create_task(press(tg,'m:report:24'))
    try:
        await asyncio.wait_for(started.wait(),2)
        await press(tg,'m:home')
        await press(tg,'m:status')
        release.set(); await task
        assert 'Радар — состояние' in tg[2].messages[(111,101)].text
        assert len(sent(tg[2]))==2 and 'Отчёт готов' in sent(tg[2])[-1].text
    finally:
        release.set(); await task


@pytest.mark.asyncio
async def test_revoked_report_access_prevents_file_delivery(ctx,tg,monkeypatch):
    async def build(*args):
        ctx.db.x("UPDATE users SET active=0 WHERE tg_id='111'")
        return report_result()
    monkeypatch.setattr(commands,'build_report',build)
    await command(tg,'/menu')
    await press(tg,'m:report:24')
    assert not [m for m in tg[2].sent if isinstance(m,SendDocument)]
    assert 'отменена' in edits(tg[2])[-1].text


@pytest.mark.asyncio
async def test_candidates_select_decide_and_home_use_single_panel(ctx,tg):
    ctx.db.upsert_source({'key':'vk_wall:-999','kind':'vk_wall','handle':'-999','title':'Новый паблик',
                         'candidate':1,'enabled':0,'members':2000,'meta':{'owner_id':-999,'n':1,'ev':[]}})
    await command(tg,'/menu')
    await press(tg,'m:cand')
    toggle=next(x for x in payloads(tg[2].messages[(111,101)]) if x.startswith('k:') and ':t1' in x)
    await press(tg,toggle)
    add=next(x for x in payloads(tg[2].messages[(111,101)]) if x.startswith('k:') and x.endswith(':add'))
    await press(tg,add)
    await press(tg,'m:home')
    assert ctx.db.scalar("SELECT candidate FROM sources WHERE key='vk_wall:-999'")==0
    assert len(sent(tg[2]))==1
