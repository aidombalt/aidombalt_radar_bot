"""Решения оператора как данные (дизайн §6): «🏠 Наш дом», «🚫 Не наш дом», «✅ Наше».

Записи — в config/entities.learned.yaml (пользовательский файл, обновление архива его не затирает); справочник
подмешивает их при загрузке: чужие дома — как соседей с привязкой к ЖК, наши — как адреса УК. Наш адрес из основного
справочника в «чужие» не пишется никогда.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .buildings import BLD_RE
from .entities import load_learned, parse_address, save_learned
from .textnorm import compact
from .util import iso, jload

if TYPE_CHECKING:
    from .context import Ctx

TYPE_TEXT = {"ул": "ул.", "пр": "пр.", "пр-д": "пр-д", "б-р": "б-р", "ш": "ш.", "наб": "наб.", "пер": "пер.",
             "ал": "аллея", "дор": "дор.", "пл": "пл.", "лин": "линия"}


def building_of(ctx: "Ctx", item: dict) -> dict | None:
    """Разобранный в публикации дом не из справочника: {address, label, zhk} или None."""
    m = jload(item.get("match_json"), {}) or {}
    reg = ctx.reg
    f = next((f for f in m.get("foreign") or [] if not f.get("known")), None)
    if f:
        street, house, part, zhk = f["street"], f["house"], f.get("part") or "", f.get("zhk") or ""
        label = f["label"]
    else:
        ev = next((h for h in m.get("hits", []) if h["kind"] == "street" and h["conf"] == 0.25), None)
        if not ev:
            return None
        label = ev["evidence"].replace("улица: ", "").split(" — ")[0].strip()
        bm = re.search(r"(\d{1,3}.*)$", label)
        if not bm:
            return None
        street = label[: bm.start()].strip()
        b = BLD_RE.match(bm.group(1))
        if not b:
            return None
        lit = (b.group("lit") or b.group("lit2") or "").upper()
        house = b.group("h") + lit
        pk = (b.group("pk") or "").lower()
        part = (("стр" if pk.startswith("стр") else "/" if pk == "/" else "к") + b.group("pv")) if b.group("pv") else ""
        zhits = {h.get("zhk") or h["key"] for h in m.get("hits", []) + m.get("explained_hits", []) if h["kind"] == "zhk"}
        on_street = {a.zhk for a in reg.addresses if compact(a.street) == compact(street) and a.zhk}
        zhk = next(iter(zhits & on_street), "") or (next(iter(on_street)) if len(on_street) == 1 else "")
    typ = next((a.street_type for a in reg.addresses if compact(a.street) == compact(street)), "ул")
    p = re.match(r"(стр|/|к|-)?(\d+)$", part) if part else None
    tail = ""
    if p:
        kind, val = p.group(1) or "к", p.group(2)
        tail = {"стр": f" стр. {val}", "/": f"/{val}"}.get(kind, f" к. {val}")
    address = f"{TYPE_TEXT.get(typ, typ + '.')} {street}, д. {house}{tail}"
    return {"address": address, "label": label, "zhk": zhk}


def mark_not_ours(ctx: "Ctx", item_id: int, user: str) -> str:
    it = ctx.db.item(item_id)
    if not it:
        return "Публикация не найдена"
    b = building_of(ctx, dict(it))
    ours = [h["evidence"] for h in (jload(it["match_json"], {}) or {}).get("hits", []) if h["kind"] == "address" and h["conf"] >= 0.9]
    if not b and ours:
        return f"{ours[0].replace('адрес: ', '')} — наш адрес по справочнику. Если это ошибка, поправьте справочник в GUI → «Объекты»"
    if not b:
        ctx.alerts.ack(item_id, user, "false_positive")
        return "Дом в тексте не разобран — отмечено «Ложное» (копии этой истории не будут будить)"
    pa = parse_address(b["address"])
    if pa and any(a.key == pa.key for a in ctx.reg.addresses):
        return f"{b['label']} — наш адрес по справочнику. Если это ошибка, поправьте справочник в GUI → «Объекты»"
    data = load_learned()
    if not any(x.get("address") == b["address"] for x in data["foreign_buildings"]):
        data["foreign_buildings"].append({"address": b["address"], "zhk": b["zhk"], "note": f"«🚫 Не наш» в карточке #{item_id}",
                                          "by": user, "at": iso(), "item": item_id})
        save_learned(data)
    ctx.alerts.ack(item_id, user, "false_positive")
    note = f"чужой дом: {b['label']}"
    ctx.db.x("UPDATE feedback SET note=? WHERE id=(SELECT MAX(id) FROM feedback WHERE item_id=?)", (note, item_id))
    ctx.db.x("UPDATE items SET operator_note=? WHERE id=?", (note, item_id))
    ctx.reload_registry(recover=False)
    ctx.pipeline.reconcile_down()
    return f"Записано: {b['label']} — не наш дом. Дальше такие публикации — в отчёт, без алертов"


def companies_of(ctx: "Ctx", zhk: str) -> list[str]:
    return sorted({a.company for a in ctx.reg.addresses if a.zhk == zhk and a.company})


def mark_ours(ctx: "Ctx", item_id: int, user: str, company: str | None = None) -> tuple[str, list[str]]:
    """«🏠 Наш дом». Если в ЖК несколько наших УК и УК не выбрана — вернуть список для выбора."""
    it = ctx.db.item(item_id)
    if not it:
        return "Публикация не найдена", []
    b = building_of(ctx, dict(it))
    if not b:
        return "Дом в тексте не разобран — добавьте адрес в GUI → «Объекты»", []
    cos = companies_of(ctx, b["zhk"]) if b["zhk"] else []
    if not company:
        if len(cos) > 1:
            return f"{b['label']}: какая наша УК обслуживает дом?", cos
        company = cos[0] if cos else ""
    if company not in ctx.reg.companies:
        return "УК не определена — добавьте адрес в GUI → «Объекты»", []
    data = load_learned()
    data["foreign_buildings"] = [x for x in data["foreign_buildings"] if x.get("address") != b["address"]]
    if not any(x.get("address") == b["address"] for x in data["our_buildings"]):
        data["our_buildings"].append({"address": b["address"], "company": company, "zhk": b["zhk"], "by": user,
                                      "at": iso(), "item": item_id})
    save_learned(data)
    ctx.memory.feedback(item_id, "our_building", user, f"наш дом: {b['label']}")
    ctx.reload_registry(recover=True)             # проход «вверх»: связанные публикации вернутся (§5.4)
    return f"Записано: {b['label']} — наш дом ({ctx.reg.company_name(company)}). Связанные публикации пересмотрены", []


def mark_item_ours(ctx: "Ctx", item_id: int, user: str) -> str:
    """«✅ Наше» на подавленной публикации: белый корень, потолок без объяснения и наследования, проход «вверх»."""
    if not ctx.db.item(item_id):
        return "Публикация не найдена"
    ctx.memory.feedback(item_id, "ours", user)
    ctx.db.x("UPDATE items SET workflow='open' WHERE id=? AND workflow='false_positive'", (item_id,))
    ups = ctx.pipeline.reconcile_up("✅ Наше")
    return "Отмечено «наше»" + (f": возвращено алертами {len(ups)}" if ups else "")
