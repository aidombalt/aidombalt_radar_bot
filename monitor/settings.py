"""Конфигурация: config/settings.yaml + .env (секреты) + оперативные правки из GUI/бота (таблица kv)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = Path(os.environ.get("RADAR_DATA_DIR", ROOT / "data"))

# Файлы, которые пользователь правит из GUI/бота: в архиве лежат как *.example.yaml и копируются только при отсутствии,
# чтобы распаковка обновления поверх папки не стёрла добавленных людей.
USER_CONFIGS = ("watchlist.yaml",)


def ensure_user_configs() -> None:
    for name in USER_CONFIGS:
        dst, src = CONFIG_DIR / name, CONFIG_DIR / name.replace(".yaml", ".example.yaml")
        if not dst.exists() and src.exists():
            dst.write_text(src.read_text("utf-8"), "utf-8")


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    try:    # Блокнот Windows сохраняет то с BOM, то в cp1251 — читаем оба
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        text = path.read_text(encoding="cp1251")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


class Schedule(BaseModel):
    hot_minutes: int = 5          # губернаторы, надзор, инфлюенсеры, «горячие» посты
    warm_minutes: int = 15        # поиск ВК, СМИ, Telegram-каналы
    cold_minutes: int = 180       # автопоиск пабликов, «доктор источников», госпорталы
    reports: list[str] = ["09:00", "14:00", "19:00"]   # МСК; отчёт уходит даже при нуле упоминаний
    work_hours: str = "09:00-18:00"                    # рабочее время PR (МСК, пн–пт): что было вне его — в отчёте отдельно
    weekly_report: str = "MON 09:30"
    track_hours: int = 48         # сколько часов следим за динамикой лайков/комментов у значимых постов
    max_age_days: int = 3         # старше — в архив без анализа (при первом опросе канала приходят посты за месяцы)


class Alerts(BaseModel):
    instant_priorities: list[str] = ["P1", "P2"]
    escalate_after_min: int = 15            # P1 без «Взято в работу» → повтор + эскалация
    repeat_p1_every_min: int = 15
    max_escalations: int = 4
    telegram_fail_to_relay_after: int = 3   # после N неудачных попыток отдаём доставку Colab-ретранслятору
    downtime_notice_min: int = 60           # Радар не работал дольше (сон/выключение ноутбука) — сообщить по возвращении
    night_hold: bool = True                 # вне рабочего времени P2 без надзора/СМИ/массовой аварии — к началу рабочего дня
    late_hours: float = 12                  # опубликовано раньше, чем Радар увидел (новый источник, перерыв) — одной сводкой
    # Закрытые карточки убираются из чатов Telegram и Max (вместе с напоминаниями и ответами «Черновик»/«Динамика»).
    # Пауза — чтобы успеть нажать «↩️ Вернуть». 0 — не убирать. Telegram удаляет только сообщения моложе 48 ч.
    cleanup_false_min: int = 30             # «❌ Ложное» и «⚪ Снято автоматически»
    cleanup_resolved_min: int = 120         # «🟢 Решено»


class Risk(BaseModel):
    influencer_followers: int = 5000
    p1_score: float = 70
    p2_score: float = 45
    p3_score: float = 20
    min_match_conf: float = 0.45            # ниже — не кандидат (если нет гео/жалобного сигнала)
    # «Потолки» приоритета по доказательствам в ТЕКСТЕ (не по догадкам модели): без нашего объекта — не будим людей
    # Наша улица без номера дома. 23.10: «дом на проспекте Обуховской Обороны» (СК) пришёл P2 «наш ЖК Дом-Мегалит»,
    # хотя наш там один дом (195) из сотен. Улица — это район, а не дом: P3, в отчёт.
    street_cap: str = "P3"
    # Улицы, где почти все дома наши (новый квартал целиком): там «улица без дома» значит «наш дом» — street_dense_cap
    street_dense: list[str] = Field(default_factory=list)   # ["Шувалова", "Графская"] — как в справочнике, без «ул.»
    street_dense_cap: str = "P2"
    # ЖК, где есть дома других УК (shared: true в справочнике), назван без нашего дома и нашей УК — P3, в отчёт.
    # 03.11: «Ждём Водоканал с проверкой в ЖК Солнечный город» пришло P2 «УК Цивилизация», хотя дом не назван.
    shared_zhk_cap: str = "P3"
    watched_cap: str = "P2"                 # автор на контроле, но без нашего адреса
    cluster_cap: str = "P3"                 # только район/сосед (Мурино, КоммуналСервис) — в отчёт, без пуша
    no_evidence_cap: str = "P4"             # ни объекта, ни адреса, ни района — фон (лента), без пуша
    foreign_cap: str = "P3"                 # в тексте назван чужой корпус нашего дома (ЖК «объяснён»), ЖК сверен или адрес известен
    registry_check_cap: str = "P2"          # то же, но справочник ЖК не сверен: «дом не из справочника — проверить» (ночью ждёт утра)
    velocity_hot_per_hour: float = 30       # (лайки+2*комменты+3*репосты)/час → «разгоняется»


class Story(BaseModel):
    """«Та же история» (дизайн §5): копии/перепосты истории, которую оператор отметил «Ложное» или которая про чужой
    дом в нашем ЖК, не будят людей; копии истории, отмеченной «наше», не дают отдельных карточек."""
    enabled: bool = True
    trial_days: int = 7                     # первые дни после обновления — утренняя сводка «склейки за сутки» на проверку
    root_days: int = 7                      # сколько дней корень истории действует
    window_hours: int = 72                  # сверщик пересматривает открытые P1/P2 за это время
    theme_min: float = 0.45                 # IDF-пересечение лемм (по меньшему тексту)
    round_theme_min: float = 0.60           # если единственный якорь — круглая сумма (100 000, 600 000)
    copy_min: float = 0.5                   # вхождение триграмм меньшего текста в больший
    money_min: int = 10000                  # суммы меньше — не идентификатор
    # Решение оператора («❌ Ложное», «🟢 Решено») держится за историю дольше и действует даже на копии, где назван
    # НАШ адрес: перепост той же жалобы в другом паблике через день не должен будить людей снова (23.10, ЖК Виктория)
    verdict_days: int = 30                  # сколько дней копии истории наследуют «Ложное»
    # «Решено» — короче: перепосты расходятся за 1–3 дня, а почти те же слова через неделю — скорее новая авария
    resolved_hours: int = 72
    verdict_copy_min: float = 0.6           # для копий с нашим адресом — только почти дословная копия (или ссылка на корень)


class LocalBrain(BaseModel):
    enabled: bool = True
    kind: str = "ollama"                    # ollama | openai
    base_url: str = "http://127.0.0.1:11434"
    model: str = "hf.co/google/gemma-4-E4B-it-qat-q4_0-gguf"
    timeout: float = 120.0
    concurrency: int = 1


class Brain(BaseModel):
    colab_offline_after_sec: int = 180
    lease_sec: int = 300
    p1_max_wait_sec: int = 120              # кандидат с сильным сигналом ждёт Colab не дольше
    normal_max_wait_sec: int = 1200         # остальные — 20 минут, дальше разбирает локальный мозг
    upgrade_hours: int = 24                 # когда Colab вернулся — переразбирает то, что делали local/rules
    pii_masking: bool = True
    agent_max_rounds: int = 2
    local: LocalBrain = LocalBrain()


class VK(BaseModel):
    api_version: str = "5.199"
    api_bases: list[str] = ["https://api.vk.com/method", "https://api.vk.ru/method"]
    rps: float = 2.5
    search_daily_budget: int = 900
    comments_lookback_hours: int = 72
    posts_per_wall: int = 20


class Network(BaseModel):
    telegram_proxy: str | None = None      # http://user:pass@host:port или socks5://… (для Bot API из РФ)
    http_proxy_foreign: str | None = None  # опционально: прокси для «зарубежных» источников с сервера
    gui_public: bool = False               # True — пускать в GUI не только из локальной сети (только за своим HTTPS+VPN!)


class Max(BaseModel):
    enabled: bool = True  # без MAX_BOT_TOKEN канал не запускается
    api_url: str = "https://platform-api2.max.ru"
    mode: Literal["polling", "webhook"] = "polling"
    fallback_from_tg: bool = True
    only_when_tg_down: bool = False
    report_files: bool = False  # явное включение передачи Excel/Word в группу
    pin_p1: bool = False
    direct_messages: bool = True
    events: list[str] = ["p1", "p2", "esc", "digest", "story", "rep_morning", "rep_weekly", "service"]
    poll_timeout: int = Field(default=30, ge=1, le=90)
    ca_bundle: str = ""  # PEM доверенного УЦ; системное хранилище дополняется, verify=False запрещён
    webhook_url: str = ""  # иначе public_url + /api/max/webhook


class Settings(BaseModel):
    company: str = "ГК «Балтийский Дом»"
    timezone: str = "Europe/Moscow"
    schedule: Schedule = Schedule()
    alerts: Alerts = Alerts()
    risk: Risk = Risk()
    story: Story = Story()
    brain: Brain = Brain()
    vk: VK = VK()
    network: Network = Network()
    max: Max = Max()
    report_emails: list[str] = Field(default_factory=list)
    gui_port: int = 8080
    public_url: str = ""                   # https://radar.example.ru — для ссылок в письмах/боте

    # --- секреты из окружения ---
    @property
    def vk_service_token(self) -> str:
        return os.environ.get("VK_SERVICE_TOKEN", "")

    @property
    def vk_user_token(self) -> str:
        return os.environ.get("VK_USER_TOKEN", "")

    @property
    def tg_bot_token(self) -> str:
        return os.environ.get("TG_BOT_TOKEN", "")

    @property
    def max_bot_token(self) -> str:
        return os.environ.get("MAX_BOT_TOKEN", "")

    @property
    def max_admin_chat_id(self) -> str:
        return os.environ.get("MAX_ADMIN_CHAT_ID", "").strip()

    @property
    def max_webhook_secret(self) -> str:
        return os.environ.get("MAX_WEBHOOK_SECRET", "")

    @property
    def bridge_token(self) -> str:
        return os.environ.get("BRIDGE_TOKEN", "")

    @property
    def gui_password(self) -> str:
        return os.environ.get("GUI_PASSWORD", "")

    @property
    def smtp(self) -> dict[str, Any]:
        return {
            "host": os.environ.get("SMTP_HOST", ""),
            "port": int(os.environ.get("SMTP_PORT", "465") or 465),
            "user": os.environ.get("SMTP_USER", ""),
            "password": os.environ.get("SMTP_PASSWORD", ""),
            "from": os.environ.get("SMTP_FROM", os.environ.get("SMTP_USER", "")),
            "starttls": os.environ.get("SMTP_STARTTLS", "0") == "1",
        }


def deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_yaml(name: str, default: Any = None) -> Any:
    p = CONFIG_DIR / name
    if not p.exists():
        return default
    with p.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or default


def load_settings(overrides: dict | None = None) -> Settings:
    _load_dotenv(Path(os.environ.get("RADAR_ENV_FILE", str(ROOT / ".env"))))
    raw = load_yaml("settings.yaml", {}) or {}
    env = {}
    if os.environ.get("LOCAL_LLM_URL"):          # docker-compose: http://ollama:11434
        env = {"brain": {"local": {"base_url": os.environ["LOCAL_LLM_URL"]}}}
    if os.environ.get("PUBLIC_URL"):
        env["public_url"] = os.environ["PUBLIC_URL"]
    return Settings.model_validate(deep_merge(deep_merge(raw, env), overrides or {}))


DATA_DIR.mkdir(parents=True, exist_ok=True)
(DATA_DIR / "reports").mkdir(exist_ok=True)
(DATA_DIR / "backups").mkdir(exist_ok=True)
