"""Read-only MAX connection check; no message sends, update reads or subscription changes."""
from .max_api import MaxError, error_hint
from ..settings import ROOT


async def check(ctx):
    s = ctx.settings
    print("MAX: проверка подключения")
    print("Файл .env рядом с run.py:", "найден" if (ROOT / ".env").is_file() else "не найден")
    print("MAX_BOT_TOKEN:", "загружен" if s.max_bot_token else "НЕ ЗАГРУЖЕН")
    print("Max включён:", s.max.enabled, "| режим:", s.max.mode, "| личные сообщения:", s.max.direct_messages)
    if not s.max_bot_token:
        print("Добавьте MAX_BOT_TOKEN в .env рядом с run.py и перезапустите Радар.")
        return False
    api = None
    ok = True
    stage = "создание HTTPS-клиента"
    try:
        api = ctx.alerts.max.api
        stage = "GET /me"
        await api.me()
        print("OK: HTTPS работает, Max принял токен.")
        if s.max_admin_chat_id:
            stage = "проверка админ-группы"
            try:
                await api.chat(s.max_admin_chat_id)
                member = await api.member(s.max_admin_chat_id)
                permissions = set(member.get("permissions") or [])
                owner = bool(member.get("is_owner"))
                admin = owner or bool(member.get("is_admin"))
                read = owner or "read_all_messages" in permissions
                write = owner or bool(permissions & {"write", "post_edit_delete_message"})
                print(f"Группа: администратор={admin}, чтение команд={read}, отправка={write}")
                ok = admin and read and write
            except MaxError as err:
                ok = False
                print(f"ОШИБКА группы: {err}; {error_hint(err)}. Личные команды проверяются отдельно.")
        else:
            print("MAX_ADMIN_CHAT_ID не задан; личные команды могут работать без группы.")
        stage = "GET /subscriptions"
        subs = (await api.subscriptions()).get("subscriptions") or []
        print("Webhook-подписок:", len(subs))
        if s.max.mode == "polling" and subs:
            print("Есть webhook: при запуске Радар должен удалить его перед переходом на polling.")
        if s.max.mode == "webhook":
            stage = "настройка webhook"
            ctx.max_bot.webhook_url()
            print("Настройки webhook заполнены; доступность сервера снаружи не проверялась.")
        print("Проверка завершена. Сообщения не отправлялись; события и настройки Max не изменялись.")
        return ok and s.max.enabled
    except MaxError as err:
        print(f"ОШИБКА: {stage}: {err}; {error_hint(err)}")
        return False
    except Exception as err:
        print(f"ОШИБКА: {stage}: {type(err).__name__}; проверьте настройки max и файл max.ca_bundle.")
        return False
    finally:
        if api is not None:
            await api.close()

