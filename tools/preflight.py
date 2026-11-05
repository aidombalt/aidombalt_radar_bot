"""Offline deployment checks; prints field names, never secret values. Exit 1 blocks startup."""
from __future__ import annotations
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    errors, warnings = [], []
    env_path = Path(os.environ.get('RADAR_ENV_FILE', str(ROOT / '.env')))
    if not env_path.is_file():
        print('ERROR: файл секретов отсутствует (RADAR_ENV_FILE или .env).')
        return 1
    if os.name == 'posix' and env_path.stat().st_mode & (stat.S_IROTH | stat.S_IWOTH | stat.S_IXOTH):
        errors.append('Файл секретов доступен остальным пользователям; установите 0640 или 0600.')
    from monitor.settings import DATA_DIR, CONFIG_DIR, load_settings
    overrides = {}
    db_path = DATA_DIR / 'radar.sqlite3'
    if db_path.exists():
        try:
            with sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True) as conn:
                row = conn.execute("SELECT value FROM kv WHERE key='settings_overrides'").fetchone()
                if row:
                    overrides = json.loads(row[0]) or {}
        except Exception:
            errors.append('Не удалось прочитать настройки из базы; проверьте перенос и целостность SQLite.')
    try:
        settings = load_settings(overrides)
    except Exception:
        print('ERROR: неверный формат settings.yaml, настроек в БД или окружения (значения скрыты).')
        return 1
    if len(settings.gui_password) < 16:
        errors.append('GUI_PASSWORD: задайте пароль длиной не менее 16 символов.')
    if settings.network.gui_public:
        errors.append('network.gui_public=true: для этой поставки требуется false (в YAML и настройках БД).')
    if not settings.vk_service_token:
        warnings.append('VK_SERVICE_TOKEN пуст: сбор ВКонтакте недоступен.')
    if not settings.tg_bot_token:
        warnings.append('TG_BOT_TOKEN пуст: Telegram-бот не запустится.')
    elif not os.environ.get('TG_ADMINS', '').strip():
        warnings.append('TG_ADMINS пуст: проверьте администраторов в перенесённой базе.')
    if settings.bridge_token and len(settings.bridge_token) < 32:
        errors.append('BRIDGE_TOKEN: используйте случайный токен длиной не менее 32 символов.')
    if not settings.bridge_token:
        warnings.append('BRIDGE_TOKEN пуст: подключение Colab выключено.')
    if os.environ.get('TUNNEL', '').strip():
        warnings.append('TUNNEL включён: проверьте, нужен ли серверу туннель от старого ноутбука.')
    if os.environ.get('RELAY_VIA_COLAB', '1') == '1':
        warnings.append('RELAY_VIA_COLAB=1: доверенный Colab получает токен Telegram-бота.')
    if settings.max_bot_token and settings.max.mode == 'webhook':
        secret = settings.max_webhook_secret
        if not 16 <= len(secret) <= 256 or not all(c.isascii() and (c.isalnum() or c in '_-') for c in secret):
            errors.append('MAX_WEBHOOK_SECRET не соответствует требованиям webhook.')
    for name in ('settings.yaml', 'entities.yaml', 'sources.yaml', 'watchlist.yaml'):
        if not (CONFIG_DIR / name).is_file():
            errors.append(f'Отсутствует config/{name}.')
    for path, label in ((DATA_DIR, 'data'), (CONFIG_DIR, 'config')):
        if not os.access(path, os.W_OK):
            errors.append(f'Нет права записи в {label}.')
    for line in warnings:
        print('WARN: ' + line)
    for line in errors:
        print('ERROR: ' + line)
    print('Проверка завершена: ' + ('есть ошибки.' if errors else 'ошибок конфигурации не найдено.'))
    print('Сеть, модель, доставка и качество анализа проверяются отдельно при приёмке.')
    return int(bool(errors))


if __name__ == '__main__':
    raise SystemExit(main())
