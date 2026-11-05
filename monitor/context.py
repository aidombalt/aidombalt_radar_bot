"""Контекст приложения: все сервисы в одном месте (БД, справочник, матчер, память, алерты, состояние мозга)."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .db import DB
from .entities import Registry, load_registry
from .matcher import Matcher
from .memory import Memory
from .settings import CONFIG_DIR, Settings, ensure_user_configs, load_settings, load_yaml
from .textnorm import normalize
from .util import jload

log = logging.getLogger("radar")


class Watch:
    """Люди/аккаунты на особом контроле (инфлюенсеры, активисты) и стоп-листы."""

    def __init__(self, data: dict):
        self.data = data or {}
        self.people = self.data.get("people", []) or []
        # author_id в базе: «vk:<число>» (ВК) и «tg:<канал>» (Telegram) — сверяем по ним, а не по короткому имени
        self.ids: dict[str, str] = {}
        for p in self.people:
            for k in ("vk_id", "tg"):
                if p.get(k):
                    self.ids[str(p[k]).lower()] = p["name"]
            if p.get("vk_uid"):
                self.ids[f"vk:{p['vk_uid']}"] = p["name"]
            if p.get("tg"):
                self.ids[f"tg:{str(p['tg']).lower()}"] = p["name"]
        self.names = [(normalize(a).lower(), p["name"]) for p in self.people
                      for a in [p["name"], *p.get("aliases", []), *([f"@{p['tg']}"] if p.get("tg") else [])]]
        self.ignore_authors = {str(x) for x in self.data.get("ignore_authors", []) or []}
        self.ignore_phrases = [normalize(x).lower() for x in self.data.get("ignore_phrases", []) or []]
        self.keywords = self.data.get("keywords", []) or []

    def author_watched(self, it: dict) -> str | None:
        aid = str(it.get("author_id") or "").lower()
        if aid and aid in self.ids:
            return self.ids[aid]
        name = normalize(it.get("author_name") or "").lower()
        text = (it.get("text") or "").lower()
        for alias, who in self.names:
            if alias and (alias in name or ((len(alias) > 8 or alias.startswith("@")) and alias in text)):
                return who
        return None

    def ignored(self, raw: dict, text: str) -> bool:
        if str(raw.get("author_id") or "") in self.ignore_authors:
            return True
        low = text.lower()
        return any(p and p in low for p in self.ignore_phrases)


@dataclass
class BrainState:
    colab_online: bool = False
    colab_info: dict = field(default_factory=dict)
    local_online: bool = False
    local_note: str = ""              # почему локальная модель недоступна (пусто — всё в порядке)
    local_checked: str | None = None   # когда проверяли (None — ещё не проверяли)
    last_colab_seen: str | None = None
    tunnel_url: str | None = None    # адрес моста для Colab (если Радар сам держит туннель)
    tunnel_probe: str = ""           # последняя проверка моста снаружи: «✅ 13:05» / «❌ HTTP 530 …»
    foreign_mode: str = "colab"      # кто опрашивает Telegram/Google News: colab | server | none
    foreign_mode: str = "colab"      # кто опрашивает Telegram/Google News: colab | server | none
    foreign_mode: str = "colab"      # кто опрашивает Telegram/Google News: colab | server | none


class Ctx:
    def __init__(self, settings: Settings | None = None, db: DB | None = None):
        ensure_user_configs()
        self.settings = settings or load_settings()
        self.db = db or DB()
        overrides = self.db.kv_get("settings_overrides", {}) or {}
        if overrides:
            self.settings = load_settings(overrides)
        self.reg: Registry = load_registry()
        self.watch = Watch(load_yaml("watchlist.yaml", {}))
        self.matcher = Matcher(self.reg, self.watch.keywords)
        self.memory = Memory(self.db, self.reg, self.settings.risk.influencer_followers, self.settings.brain.pii_masking)
        self.brain = BrainState()
        from .alerts.outbox import Alerts
        from .pipeline import Pipeline

        from .cases import Cases

        self.pipeline = Pipeline(self)
        self.alerts = Alerts(self)
        from .alerts.max_bot import MaxBot
        self.max_bot = MaxBot(self)
        self.cases = Cases(self)
        self.bot = None            # aiogram Bot (если настроен)
        self.scheduler = None
        self.collectors = {}

    def reload_registry(self, recover: bool = True) -> None:
        self.reg = load_registry()
        self.watch = Watch(load_yaml("watchlist.yaml", {}))
        self.matcher = Matcher(self.reg, self.watch.keywords)
        self.memory.reg = self.reg
        if getattr(self, "cases", None):
            self.cases.reset()
        if recover and getattr(self, "pipeline", None):
            try:      # справочник поменялся: подавленные «чужим домом»/«той же историей» — пересмотреть (дизайн §5.4)
                self.pipeline.reconcile_up("справочник изменён")
            except Exception:  # noqa: BLE001 — пересмотр не должен мешать сохранению справочника
                log.exception("пересмотр после правки справочника не удался")
        log.info("справочник перезагружен: %s", {"ук": len(self.reg.companies), "жк": len(self.reg.zhks),
                                                  "адресов": len(self.reg.addresses)})

    def set_override(self, path: str, value) -> None:
        """Оперативная настройка из GUI/бота: path вида 'schedule.hot_minutes'."""
        ov = self.db.kv_get("settings_overrides", {}) or {}
        cur = ov
        parts = path.split(".")
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = value
        self.db.kv_set("settings_overrides", ov)
        self.settings = load_settings(ov)
        self.memory.infl = self.settings.risk.influencer_followers

    def save_watchlist(self, data: dict) -> None:
        (CONFIG_DIR / "watchlist.yaml").write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), "utf-8")
        self.reload_registry()

    def fill_watch_ids(self) -> int:
        """У людей на контроле записан короткий адрес ВК (toma_rodimceva), а в публикациях автор — число (vk:123).
        Берём число из уже разрешённого источника-стены и дописываем в watchlist, чтобы узнавать их комментарии везде."""
        data = load_yaml("watchlist.yaml", {}) or {}
        n = 0
        for p in data.get("people", []) or []:
            if p.get("vk_id") and not p.get("vk_uid"):
                r = self.db.one("SELECT meta_json FROM sources WHERE key=?", (f"vk_wall:{p['vk_id']}",))
                oid = (jload(r["meta_json"], {}) or {}).get("owner_id") if r else None
                if oid and int(oid) > 0:
                    p["vk_uid"], n = int(oid), n + 1
        if n:
            self.save_watchlist(data)
        return n

    def sync_sources_from_yaml(self, path: Path | None = None) -> int:
        """Загрузить реестр источников из config/sources.yaml (состояние здоровья сохраняется)."""
        data = load_yaml("sources.yaml", {}) if path is None else yaml.safe_load(path.read_text("utf-8"))
        n = 0
        for group in (data or {}).get("groups", []):
            defaults = {k: v for k, v in group.items() if k != "sources"}
            for s in group.get("sources", []):
                src = {**defaults, **s}
                src.setdefault("title", src.get("handle"))
                src["key"] = src.get("key") or f"{src['kind']}:{src['handle']}"
                self.db.upsert_source(src)
                n += 1
        for p in self.watch.people:
            for kind, fld in (("vk_wall", "vk_id"), ("tg_channel", "tg")):
                if kind == "tg_channel" and p.get("tg_channel") is False:
                    continue        # личный аккаунт Telegram: ленты нет, отмечаем только упоминания
                if p.get(fld):
                    self.db.upsert_source({"key": f"{kind}:{p[fld]}", "kind": kind, "handle": str(p[fld]),
                                           "title": f"👤 {p['name']}", "tier": "hot", "region": "",
                                           "category": "influencer", "comments": kind == "vk_wall",
                                           "where": "colab" if kind == "tg_channel" else "server"})
                    n += 1
        return n
