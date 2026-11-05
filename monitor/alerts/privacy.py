"""Fail closed on generated affiliation claims for non-public companies.
Public source quotations remain evidence; generated analysis/drafts are filtered.
"""
from __future__ import annotations
import re
from copy import deepcopy
from ..util import jload, jdump


def protected_item(ctx, item):
    evidence = " ".join(str(item.get(key) or "") for key in ("text", "title", "tags", "match_json")).casefold()
    private = [c for key, c in ctx.reg.companies.items() if c.get("public") is False and
               any(str(alias).casefold() in evidence for alias in (key, c.get("name", ""), *c.get("aliases", [])) if alias)]
    if not private:
        return item
    analysis = deepcopy(jload(item.get("analysis_json"), {}) or {})
    group_terms = [ctx.settings.company, ctx.reg.raw.get("group", {}).get("name", ""), "Балтийский Дом"]
    changed = False
    for field in ("summary", "why", "action", "reply_draft"):
        value = analysis.get(field)
        if not isinstance(value, str):
            continue
        if any(t and t.casefold() in value.casefold() for t in group_terms) or re.search(r"\b(?:ГК|групп[аыуе]?\s+компани[йия])\b", value, re.I):
            analysis[field] = "Текст ИИ скрыт: проверьте формулировку и подготовьте ответ от имени конкретной УК."
            changed = True
    return {**item, "analysis_json": jdump(analysis)} if changed else item
