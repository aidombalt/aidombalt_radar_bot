"""Shared commands and authorized, durable operator actions for both messengers."""
from __future__ import annotations
import asyncio
import hashlib
import math
import secrets
from dataclasses import dataclass, field, asdict
from datetime import timedelta
from ..util import ago, clip, html_escape as e, iso, jload, jdump, msk, relay_url, now_utc
from ..brain.router import local_state
from ..collectors.web import gnews_pause
from .formatting import PRIO_ICON, objects_line
from .render import Card, Btn, card_for

def dynamics_text(ctx, iid: int, fresh: bool) -> str:
    db = ctx.db
    """Цифры поста (Telegram — просмотры, ВК — всё) и распространение: кто подхватил. У статей СМИ счётчиков нет —
    для них только распространение."""
    from ..spread import spread

    it = db.item(iid) or {}
    rows = db.q("SELECT ts, likes, comments, reposts, views FROM metrics WHERE item_id=? ORDER BY ts", (iid,))
    tg = it.get("platform") == "tg"
    media = it.get("platform") in ("rss", "gnews", "web")
    parts = []
    if rows:
        pick = rows if len(rows) <= 12 else [rows[0], *rows[-11:]]
        lines = [f"{msk(r['ts'], '%d.%m %H:%M')}  " + f"👁 {r['views']:>7,}".replace(",", " ")
                 + ("" if tg else f"  ♥{r['likes']} 💬{r['comments']} ↻{r['reposts']}") for r in pick]
        grow = (rows[-1]["views"] or 0) - (rows[0]["views"] or 0)
        parts.append(f"📈 <b>Динамика #{iid}</b> · {len(rows)} замер(ов)"
                     + (f" · рост просмотров +{grow:,}".replace(",", " ") if grow > 0 else "")
                     + ("" if fresh else "\n<i>свежий замер не получился — показываю последние</i>")
                     + "\n<code>" + "\n".join(lines) + "</code>"
                     + ("\n<i>Telegram не отдаёт реакции и комментарии в веб-версии — считаем просмотры.</i>" if tg else ""))
    elif media:
        parts.append(f"📈 <b>Распространение #{iid}</b>\n<i>У сайтов СМИ нет открытых счётчиков просмотров — "
                     "смотрим, кто подхватил публикацию.</i>")
    else:
        parts.append(f"📈 <b>Динамика #{iid}</b>: площадка не отдала цифры — ниже распространение.")
    sp = spread(ctx, it)
    if sp["reprints"]:
        parts.append(f"🔁 <b>Перепечатали или пересказали: {len(sp['reprints'])}</b>\n" + "\n".join(
            f"• {msk(r['at'], '%d.%m %H:%M')} — <a href=\"{e(r['url'] or '')}\">{e(clip(r['source'], 40))}</a>"
            + (f" · 👁 {r['views']:,}".replace(",", " ") if r["views"] else "") for r in sp["reprints"][:8]))
    else:
        parts.append("🔁 Перепечаток и пересказов пока нет.")
    parts.append(f"📰 По той же теме в том же районе после неё: {sp['story_n']} публ."
                 + (f" (последняя — {e(sp['story_last'])}, {msk(sp['story_at'])})" if sp["story_n"] else "")
                 + ("\n<b>Вывод:</b> не разошлась." if not sp["reprints"] and not sp["story_n"] and not rows else ""))
    return "\n\n".join(parts)


def status_text(ctx) -> str:
    db = ctx.db
    st = db.stats()
    last = st["last_run"] or {}
    colab = "🟢 онлайн" if ctx.brain.colab_online else "⚪️ офлайн"
    info = ctx.brain.colab_info or {}
    lines = [
        "<b>📊 Радар — состояние</b>",
        f"Последний цикл: {msk(last.get('finished_at'))} ({last.get('tier', '—')}), новых {last.get('n_new', 0)}",
        f"За 24 ч: собрано {st['items_24h']}, кандидатов {st['cand_24h']}",
        f"🔴 P1: {st['p1_24h']} · 🟠 P2: {st['p2_24h']} · 🟡 P3: {st['p3_24h']}",
        f"📡 Площадки: {st['sources']} (не отвечают {st['sources_bad']})"
        + (f", поиск ВК: {st['queries_vk']} запросов" if st["queries_vk"] else ""),
        "📰 Google News: " + (f"⏸ пауза до {msk(gn)} — ограничивает частые запросы с релея" if (gn := gnews_pause(ctx))
                              else f"🟢 {st['queries_gnews']} запросов по кругу"),
        f"🧠 Colab: {colab} {e(info.get('model', ''))} {e(info.get('gpu', ''))}",
        f"🧠 Локальная модель: {e(local_state(ctx))}",
        "✈️ Telegram-каналы: " + {"colab": "через Colab", "server": f"опрашивает ноутбук {'через релей' if relay_url() else 'напрямую'} (Colab офлайн)",
                                  "none": "⚠️ не опрашиваются: Colab офлайн, t.me с сервера закрыт"}.get(
            ctx.brain.foreign_mode, "—"),
        f"📥 Очередь ИИ: {st['queue']['queued']} (срочных {st['queue']['urgent']}), в работе {st['queue']['leased']}",
        "📨 Алерты в Telegram: " + ("🔴 не проходят — дублируются почтой" if ctx.alerts.tg_ok is False else
                                    "🟢 доходят" + (" (через релей)" if relay_url() else "")),
    ]
    lines.append(max_status(ctx))
    return "\n".join(lines)


def item_short(ctx, it) -> str:
    db = ctx.db
    a = jload(it["analysis_json"], {}) or {}
    return (f"{PRIO_ICON.get(it['priority'], '⚪')} <b>#{it['id']}</b> {msk(it['published_at'])} · "
            f"{e(objects_line(dict(it)))}\n{e(clip(a.get('summary') or it['text'], 220))}\n"
            f"<a href=\"{e(it['url'] or '')}\">ссылка</a> · /item {it['id']}")



@dataclass
class Actor:
    user_id: str
    name: str
    role: str
    channel: str


@dataclass
class ActionResult:
    notification: str
    item_id: int | None = None
    state: str | None = None
    reply_html: str = ""
    choices: list[str] = field(default_factory=list)
    duplicate: bool = False


def max_status(ctx):
    if not ctx.settings.max.enabled or not ctx.settings.max_bot_token:
        return "Max: ⚪ выключен (MAX_BOT_TOKEN не задан или max.enabled=false)"
    status = ctx.db.kv_get("max_health", {})
    return "Max: " + e(status.get("text", "⚪ самопроверка ещё не выполнена"))


def brain_text(ctx):
    info = ctx.brain.colab_info or {}
    return (f"<b>🧠 Мозг</b>\nColab: {'🟢' if ctx.brain.colab_online else '⚪️'} последний контакт {msk(ctx.brain.last_colab_seen)}\n"
            f"Модель: {e(info.get('model', '—'))} · GPU: {e(info.get('gpu', '—'))} · движок: {e(info.get('engine', '—'))}\n"
            f"CU/час ≈ {info.get('cu_per_hour', '—')} · сессия: {info.get('uptime_min', '—')} мин\n"
            f"Локальная: {e(local_state(ctx))}\n"
            + (f"🔗 Мост для Colab (RADAR_URL): <code>{e(ctx.brain.tunnel_url)}</code>\n" if ctx.brain.tunnel_url else "")
            + (f"Проверка моста снаружи: {e(ctx.brain.tunnel_probe)}\n" if ctx.brain.tunnel_probe else "")
            + f"Очередь: {ctx.db.queue_stats()}")


def link_code(ctx, actor):
    if actor.channel != "Telegram" or actor.role not in ("viewer", "manager", "admin"):
        raise ValueError("Код выдаётся зарегистрированному пользователю в личке Telegram")
    code = secrets.token_urlsafe(18)
    with ctx.db.tx():
        ctx.db.x("INSERT OR IGNORE INTO users(tg_id,name,role,active,added_at) VALUES(?,?,?,1,?)",
                 (actor.user_id, actor.name, actor.role, iso()))
        ctx.db.x("DELETE FROM account_links WHERE tg_id=? OR expires_at<?", (actor.user_id, iso()))
        ctx.db.x("INSERT INTO account_links(digest,tg_id,expires_at) VALUES(?,?,?)",
                 (hashlib.sha256(code.encode()).hexdigest(), actor.user_id, iso(now_utc()+timedelta(minutes=10))))
    return code


def redeem_link(ctx, code, max_uid):
    if not str(max_uid).isdigit() or not 1 <= int(max_uid) <= 9223372036854775807:
        return "Некорректный Max ID"
    digest = hashlib.sha256(code.encode()).hexdigest()
    with ctx.db.tx():
        row = ctx.db.one("SELECT * FROM account_links WHERE digest=? AND used_at IS NULL AND expires_at>?", (digest, iso()))
        if not row:
            return "Код неверен или истёк. Получите новый через /link в Telegram"
        user = ctx.db.one("SELECT * FROM users WHERE tg_id=? AND active=1", (row["tg_id"],))
        owner = ctx.db.one("SELECT tg_id FROM users WHERE max_user_id=?", (str(max_uid),))
        if not user or (owner and owner["tg_id"] != row["tg_id"]) or (user["max_user_id"] and user["max_user_id"] != str(max_uid)):
            return "Аккаунт уже привязан или отключён. Изменить привязку может администратор в GUI"
        ctx.db.x("UPDATE users SET max_user_id=? WHERE tg_id=?", (str(max_uid), row["tg_id"]))
        ctx.db.x("UPDATE account_links SET used_at=? WHERE digest=?", (iso(), digest))
    return "✅ Аккаунт Max привязан. Роль: " + user["role"]


def _mutate(ctx, payload, actor):
    parts = payload.split(":")
    if len(parts) not in (3, 4) or parts[0] not in ("a", "h") or not parts[2].isdigit():
        return ActionResult("Неизвестная кнопка")
    kind, action, iid = parts[0], parts[1], int(parts[2])
    allowed = {"a": {"ack", "res", "fp", "esc", "undo"}, "h": {"our", "not", "ours"}}
    if action not in allowed[kind]:
        return ActionResult("Неизвестная кнопка")
    if actor.role not in ("manager", "admin"):
        return ActionResult("Только менеджер или администратор")
    it = ctx.db.item(iid)
    if not it:
        return ActionResult("Не найдено")
    last = ctx.db.one("SELECT * FROM feedback WHERE item_id=? ORDER BY id DESC LIMIT 1", (iid,))
    prefix = ""
    if last:
        labels = {"in_work": "в работе", "resolved": "решено", "false_positive": "ложное", "escalated": "эскалировано", "reopened": "возвращено"}
        prefix = f"Уже отмечено: {labels.get(last['verdict'], last['verdict'])} ({last['user']}, {msk(last['ts'], '%H:%M')}). "
    if kind == "h":
        from .. import learned
        choices = []
        if action == "not":
            msg = learned.mark_not_ours(ctx, iid, actor.name)
        elif action == "our":
            msg, choices = learned.mark_ours(ctx, iid, actor.name, parts[3] if len(parts) > 3 else None)
        else:
            msg = learned.mark_item_ours(ctx, iid, actor.name)
        result = ActionResult(prefix + msg, iid, choices=choices)
    else:
        wf = {"ack": "in_work", "res": "resolved", "fp": "false_positive"}.get(action)
        if wf:
            ctx.alerts.ack(iid, actor.name, wf)
        elif action == "esc":
            ctx.memory.feedback(iid, "escalated", actor.name)
            ctx.alerts.raise_alert(iid, it["priority"] or "P1", escalation=9)
        else:
            ctx.alerts.reopen(iid, actor.name)
        msg = {"ack": "Взято в работу", "res": "Решено — карточка свёрнута", "fp": "Ложное — свёрнуто, пример ушёл в память",
               "esc": "Отправлено руководителям", "undo": "Возвращено в работу"}[action]
        result = ActionResult(prefix + msg, iid, "escalated" if action == "esc" else None)
    after = ctx.db.one("SELECT id FROM feedback WHERE item_id=? ORDER BY id DESC LIMIT 1", (iid,))
    if after and (not last or after["id"] != last["id"]):
        ctx.db.x("UPDATE feedback SET note=COALESCE(note,'') || ? WHERE id=?", (f" [channel:{actor.channel}]", after["id"]))
    ctx.alerts.queue_refresh(iid, result.state)
    return result


async def handle_action(ctx, payload, actor, event_key="", service=None):
    if actor.role not in ("viewer", "manager", "admin"):
        return ActionResult(f"Нет доступа, ваш id {actor.user_id} — передайте администратору")
    parts = payload.split(":")
    if len(parts) == 3 and parts[0] == "a" and parts[2].isdigit() and parts[1] in ("rep", "dyn", "card"):
        iid = int(parts[2]); it = ctx.db.item(iid)
        if not it:
            return ActionResult("Не найдено")
        if parts[1] == "card":
            return ActionResult("Карточка", iid)
        if parts[1] == "rep":
            from .privacy import protected_item
            it = protected_item(ctx, it)
            a = jload(it.get("analysis_json"), {}) or {}
            return ActionResult("Черновик", iid, reply_html="✍️ <b>Черновик ответа от УК</b> (проверьте факты перед публикацией):\n\n" +
                                e(a.get("reply_draft") or "ИИ не подготовил черновик — откройте карточку в GUI."))
        fresh = False
        if service:
            try:
                fresh = await asyncio.wait_for(service.refresh_item_metrics(iid), 25)
            except Exception:
                pass
        return ActionResult("Динамика", iid, reply_html=dynamics_text(ctx, iid, fresh))
    # No await between BEGIN and COMMIT: action, feedback, refresh intent and receipt are atomic.
    with ctx.db.tx():
        if event_key:
            previous = ctx.db.one("SELECT result_json FROM action_receipts WHERE channel=? AND event_key=?", (actor.channel, event_key))
            if previous:
                result = ActionResult(**jload(previous["result_json"], {})); result.duplicate = True
                return result
        result = _mutate(ctx, payload, actor)
        if event_key:
            ctx.db.x("INSERT INTO action_receipts(channel,event_key,result_json,created_at) VALUES(?,?,?,?)",
                     (actor.channel, event_key, jdump(asdict(result)), iso()))
    return result


HELP = ("<b>Команды Радара</b>\n/menu — управление кнопками\n/status — состояние · /hot — P1/P2 за сутки\n"
        "/feed [N] — лента · /item ID — карточка · /find текст — поиск\n"
        "/skipped [ч] — подавленные публикации · /brain — состояние ИИ\n"
        "/scan hot|warm — сбор · /report [ч] — отчёт\n/sources — источники · /cand — кандидаты\n"
        "/settings — отправки в группу (администратор в личке)\n/chatid — ID группы · /link код — привязка (только в личке)")
ADMIN_COMMANDS = {"src_add", "src_on", "src_off", "watch", "unwatch", "set", "users", "adduser", "deluser"}


def chunks(parts, empty="Пусто."):
    if not parts:
        return [Card(empty)]
    out, buf = [], ""
    for p in parts:
        if buf and len(buf) + len(p) > 3600:
            out.append(Card(buf)); buf = ""
        buf += p + "\n\n"
    if buf:
        out.append(Card(buf))
    return out


def report_hours(args):
    hours = float(args or 24)
    if not math.isfinite(hours) or not 0 < hours <= 168:
        raise ValueError("invalid hours")
    return hours


async def build_report(ctx, hours, service=None):
    from ..reports import builder
    return await builder.build(ctx, hours=hours, kind="manual", send=False, router=getattr(service, "router", None))


async def command(ctx, name, args, actor, service=None, private=True):
    db = ctx.db
    if actor.role not in ("viewer", "manager", "admin"):
        return [Card(f"Нет доступа, ваш id {e(actor.user_id)} — передайте администратору")]
    if name in ADMIN_COMMANDS:
        if not private or actor.role != "admin":
            return [Card("Эта команда доступна только администратору в личке")]
        return await admin_command(ctx, name, args, actor, service)
    if name in ("scan", "report") and actor.role not in ("manager", "admin"):
        return [Card("Только менеджер или администратор")]
    if name == "status":
        return [Card(status_text(ctx))]
    if name == "brain":
        return [Card(brain_text(ctx))]
    if name in ("help", "start"):
        return [Card(HELP)]
    if name in ("hot", "feed", "find"):
        if name == "hot":
            rows = db.q("SELECT * FROM items WHERE analyzed_at>=? AND priority IN ('P1','P2') ORDER BY analyzed_at DESC LIMIT 20", (ago(hours=24),))
        elif name == "feed":
            n = min(30, max(1, int(args))) if args.isdigit() else 10
            rows = db.q("SELECT * FROM items WHERE status='analyzed' AND relevant=1 ORDER BY analyzed_at DESC LIMIT ?", (n,))
        else:
            from ..textnorm import lemma_string
            rows = db.fts_search(lemma_string(args), 10) if args else []
        return chunks([item_short(ctx, x) if x["priority"] else f"#{x['id']} {e(clip(x['text'], 200))}" for x in rows],
                      "За сутки P1/P2 нет ✅" if name == "hot" else "Пусто.")
    if name == "item":
        if not args.isdigit():
            return [Card("Укажите номер: /item 123")]
        it = db.item(int(args))
        if not it:
            return [Card("Не найдено")]
        if not it.get("priority"):
            return [Card(f"#{it['id']} ещё в очереди ИИ. Текст: {e(clip(it['text'], 500))}")]
        return [card_for(ctx, it["id"], links=actor.channel == "Max")]
    if name == "skipped":
        h = min(168, max(1, int(args))) if args.isdigit() else 24
        rows = db.q("SELECT * FROM items WHERE analyzed_at>=? AND priority IN ('P3','P4') AND "
                    "json_extract(analysis_json, '$._risk.cap_kind') IN ('foreign','registry_check','same_case') "
                    "ORDER BY analyzed_at DESC LIMIT 15", (ago(hours=h),))
        if not rows:
            return [Card(f"За {h} ч подавленных «чужим домом» и копиями историй нет ✅")]
        lines = [f"🧩 <b>Не присланы за {h} ч: {len(rows)}</b> (чужой дом в нашем ЖК / копия отмеченной истории)"]
        for x in rows:
            why = ((jload(x["analysis_json"], {}) or {}).get("_risk", {}).get("combine") or "").split("не выше ")[-1][:150]
            lines.append(f"• <b>#{x['id']}</b> {e(clip(x['text'] or '', 90))}\n  <i>{e(why)}</i>")
        return [Card("\n".join(lines), [[Btn(f"✅ Наше #{x['id']}", f"h:ours:{x['id']}")] for x in rows[:10]])]
    if name == "scan":
        tiers = ("hot", "warm", "cold") if actor.channel == "Telegram" else ("hot", "warm")
        if (args or "hot") not in tiers:
            return [Card("Формат: /scan " + "|".join(tiers))]
        ok = service and service.run_now(args or "hot")
        return [Card("🔎 Сбор запущен, результаты придут алертами." if ok else "Сбор уже идёт.")]
    if name == "report":
        try:
            hours = report_hours(args)
        except ValueError:
            return [Card("Укажите период от 0 до 168 часов: /report 24")]
        from ..reports import builder
        res = await build_report(ctx, hours, service)
        return [Card(res["short"], files=[res["xlsx"], res["docx"]])]
    return [Card("Неизвестная команда. /help — список команд")]


async def admin_command(ctx, name, args, actor, service):
    import yaml
    db = ctx.db
    p = args.split()
    if name == "set":
        values = args.split(maxsplit=1)
        # No arbitrary URL/secret/GUI-security changes from a messenger.
        allowed = {"schedule.hot_minutes", "schedule.warm_minutes", "alerts.night_hold", "max.only_when_tg_down",
                   "max.report_files", "max.pin_p1", "max.fallback_from_tg", "alerts.cleanup_false_min",
                   "alerts.cleanup_resolved_min"}
        if len(values) != 2 or values[0] not in allowed:
            return [Card("Доступные настройки: " + ", ".join(sorted(allowed)))]
        try:
            value = yaml.safe_load(values[1])
        except yaml.YAMLError:
            return [Card("Не понял значение")]
        if values[0].startswith("alerts.cleanup_"):
            if type(value) is not int or not 0 <= value <= 2880:
                return [Card("Через сколько минут убирать: от 0 (не убирать) до 2880")]
        elif values[0].endswith("minutes"):
            if type(value) not in (float, int) or not 1 <= value <= 1440:
                return [Card("Интервал должен быть от 1 до 1440 минут")]
        elif type(value) is not bool:
            return [Card("Значение: true или false")]
        ctx.set_override(values[0], value)
        if service:
            service._build_jobs()
        return [Card("✅ Настройка сохранена")]
    if name in ("src_on", "src_off"):
        n = db.x("UPDATE sources SET enabled=? WHERE key=?", (int(name == "src_on"), args.strip()))
        return [Card("Готово" if n else "Нет такого источника")]
    if name == "src_add":
        if len(p) < 2 or p[0] not in ("vk", "tg", "rss"):
            return [Card("Формат: /src_add vk|tg|rss адрес [hot|warm|cold]")]
        kind = {"vk": "vk_wall", "tg": "tg_channel", "rss": "rss"}[p[0]]
        handle = p[1].replace("https://vk.com/", "").replace("https://t.me/", "").lstrip("@")
        tier = p[2] if len(p) > 2 and p[2] in ("hot", "warm", "cold") else "warm"
        db.upsert_source({"key": f"{kind}:{handle}", "kind": kind, "handle": handle, "title": handle, "tier": tier,
                          "category": "public", "comments": kind == "vk_wall", "where": "colab" if kind == "tg_channel" else "server"})
        return [Card("✅ Источник добавлен")]
    if name in ("watch", "unwatch"):
        from ..settings import load_yaml
        data = load_yaml("watchlist.yaml", {}) or {}
        if not args.strip():
            return [Card("На контроле: " + e(", ".join(x["name"] for x in data.get("people", [])) or "никого"))]
        if name == "watch":
            if not any(x["name"].lower() == args.lower() for x in data.get("people", [])):
                data.setdefault("people", []).append({"name": args[:150], "aliases": [], "note": f"добавил {actor.name}"})
        else:
            data["people"] = [x for x in data.get("people", []) if x["name"].lower() != args.lower()]
        ctx.save_watchlist(data)
        return [Card("✅ Список обновлён")]
    if name == "users":
        return chunks([f"{e(u['tg_id'])} · {e(u['name'] or '')} · {e(u['role'])} · Max: {e(u['max_user_id'] or '—')}" for u in db.q("SELECT * FROM users")])
    if name == "adduser":
        if len(p) < 2 or not p[0].isdigit() or p[1] not in ("viewer", "manager", "admin"):
            return [Card("Формат: /adduser Telegram_ID viewer|manager|admin [email]. Max ID привяжите в GUI или через /link")]
        db.x("INSERT INTO users(tg_id,name,role,email,level,active,added_at) VALUES(?,?,?,?,?,1,?) ON CONFLICT(tg_id) "
             "DO UPDATE SET role=excluded.role,email=excluded.email,level=excluded.level,active=1",
             (p[0], p[0], p[1], p[2] if len(p)>2 else "", 2 if p[1] != "viewer" else 1, iso()))
        return [Card("✅ Пользователь добавлен")]
    if name == "deluser":
        db.x("UPDATE users SET active=0 WHERE tg_id=?", (args.strip(),))
        return [Card("Отключён")]
    return [Card("Неизвестная команда")]
