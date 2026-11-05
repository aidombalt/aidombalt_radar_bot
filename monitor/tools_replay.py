"""Ретро-проверка: «что бы сказал Радар об этом тексте?» — без записи в базу и без алертов.
Используется в боте (/replay), GUI (Мозг → Проверить текст) и CLI (python -m tools.replay)."""
from __future__ import annotations

from typing import TYPE_CHECKING

from . import risk, rules
from .brain.core import analyze
from .util import html_escape as e, iso, jdump

if TYPE_CHECKING:
    from .context import Ctx


async def replay(ctx: "Ctx", service, text: str, platform: str = "vk_comment", category: str = "governor",
                 followers: int = 0) -> dict:
    m = ctx.matcher.match(text, source_category=category)
    hint = ctx.matcher.hint(m)
    item = {"id": 0, "platform": platform, "text": text, "title": "", "match_json": jdump(m.to_json()),
            "match_hint": hint, "author_followers": followers, "published_at": iso(), "likes": 0, "comments": 0,
            "reposts": 0, "views": 0, "source_key": "replay", "author_name": "проверка"}
    level, analysis, err = "rules", None, ""
    router = getattr(service, "router", None)
    if router is not None and await router.local_online():
        try:
            pkg = ctx.memory.package(item, ctx.reg.catalog_text(), {"title": "ретро-проверка", "category": category})
            analysis = await analyze(pkg, router.local, None, max_rounds=0, company=ctx.settings.company)
            level = "local"
        except Exception as ex:
            err = str(ex)
    if analysis is None:
        analysis = rules.analyze(item, m.to_json(), hint)
    score, factors = risk.score(item, m.to_json(), analysis, ctx.settings.risk, 0, 0, category)
    prio, why = risk.combine(analysis["priority"], risk.priority_from_score(score, ctx.settings.risk),
                             analysis["relevant"], level)
    cap, evidence = ctx.pipeline.evidence_cap(item, m.to_json(), analysis)    # те же потолки, что в бою
    if risk.ORDER.get(prio, 4) < risk.ORDER.get(cap, 4):
        prio, why = cap, f"{why} → не выше {cap}: {evidence}"
    return {"candidate": m.candidate, "urgency": m.urgency, "reason": m.reason, "hint": hint, "analysis": analysis,
            "level": level, "score": score, "factors": factors, "priority": prio, "why": why, "error": err}


async def replay_text(ctx: "Ctx", service, text: str) -> str:
    r = await replay(ctx, service, text)
    hot = ctx.settings.schedule.hot_minutes
    eta = f"≈{hot}–{hot * 2} мин (горячий цикл + разбор)" if r["priority"] in ("P1", "P2") else "в ближайший отчёт"
    a = r["analysis"]
    return (f"<b>Ретро-проверка</b> (как комментарий на странице губернатора)\n"
            f"Кандидат: {'✅ да' if r['candidate'] else '❌ нет'} · срочность: {'высокая' if r['urgency'] else 'обычная'}"
            f"{' · ' + e(r['reason']) if r['reason'] else ''}\n"
            f"Матчер: {e(r['hint'][:600])}\n"
            f"Итог: <b>{r['priority']}</b> (риск {r['score']:.0f}/100, {e(r['why'])})\n"
            f"Факторы: {e(', '.join(r['factors']))}\n"
            f"Суть: {e(a.get('summary', ''))}\nДействие: {e(a.get('action', ''))}\n"
            f"Анализ: {'локальная модель' if r['level'] == 'local' else 'правила (ИИ недоступен)'}\n"
            f"Алерт ушёл бы: {eta}")
