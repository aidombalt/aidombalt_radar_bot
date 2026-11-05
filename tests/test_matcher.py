"""«Грязные» кейсы для матчера — всё, на чём ломаются наивные фильтры по ключевым словам."""
import pytest

from monitor.entities import load_registry
from monitor.matcher import Matcher


@pytest.fixture(scope="module")
def mt():
    return Matcher(load_registry())


def kinds(m):
    return {(h.kind, h.key) for h in m.hits if h.conf >= 0.45}


def test_rodimtseva_style_to_governor(mt):
    m = mt.match("Александр Юрьевич, обратите внимание на ЖК Лампо! Возрождение Северо-Запада ничего не делает, "
                 "двор в грязи, лифты стоят. Мурино, Екатерининская 11.", source_category="governor")
    assert "vozrozhdenie" in m.companies() and "lampo" in m.zhks()     # непубличная, но наша УК
    assert "murino" in m.clusters and "gov_lo" in m.addressees and m.candidate and m.urgency == 1


def test_informal_address_without_names(mt):
    m = mt.match("Управляйка на Авиаторов Балтики 9к1 вторую неделю не может починить лифт, сколько можно!")
    assert ("address", "авиаторовбалтики|9|1") in kinds(m)
    assert "baltdom" in m.companies() and m.urgency == 1


def test_theatre_is_not_us(mt):
    m = mt.match("Вчера в театре «Балтийский дом» была премьера спектакля, зрители в восторге")
    assert not m.ours and not m.candidate


def test_name_victoria_is_not_zhk(mt):
    m = mt.match("Виктория, с днём рождения! Счастья и любви")
    assert not m.ours and not m.candidate


def test_zhk_victoria_with_prefix(mt):
    m = mt.match("В ЖК Виктория опять не убран двор, мусор неделю")
    assert ("zhk", "victoria") in kinds(m) and m.candidate


def test_hashtag(mt):
    m = mt.match("Опять течёт крыша #жкцветарадуги #мурино")
    assert ("zhk", "rainbow") in kinds(m)


def test_typo_fuzzy(mt):
    m = mt.match("Жители Артлайн в Приморскм жалуются на парковку")
    assert ("zhk", "artline") in kinds(m)


def test_moscow_city_vs_zhk(mt):
    assert not mt.match("Москва — столица России, сегодня там дождь").ours
    m = mt.match("В ЖК «Москва» на Предпортовом опять не работает лифт")
    assert ("zhk", "moskva") in kinds(m)


def test_house_korpus(mt):
    m = mt.match("Екатерининская 22 корпус 2 — воды нет третий день!!")
    assert ("address", "екатерининская|22|2") in kinds(m) and "primdom" in m.companies()


def test_lamps_are_not_lampo(mt):
    m = mt.match("Лампы в подъезде не горят уже неделю")
    assert "lampo" not in m.zhks()


def test_realtor_etazhi_is_not_us(mt):
    m = mt.match("Агентство недвижимости Этажи предлагает квартиры в Мурино, ипотека от 6%")
    assert not any(h.kind == "company" and h.key == "etazhi" and h.conf >= 0.45 for h in m.hits)


def test_uk_etazhi_is_us(mt):
    m = mt.match("УК Этажи в Мурино — позор, диспетчер не отвечает")
    assert ("company", "etazhi") in kinds(m)


def test_latin_lampo(mt):
    m = mt.match("Lampo Murino: residents complain")
    assert "lampo" in m.zhks()


def test_geo_complaint_on_governor_page_without_names(mt):
    m = mt.match("Александр Юрьевич, в Мурино на Шувалова управляющая компания совсем обнаглела, никто не убирает",
                 source_category="governor")
    assert m.candidate and "murino" in m.clusters and m.urgency == 1


def test_positive_is_low_urgency(mt):
    m = mt.match("Спасибо УК Приморский Дом, оперативно починили домофон в ЖК Виктория")
    assert m.candidate and m.urgency == 0


def test_neighbor_uk_still_tracked(mt):
    m = mt.match("УК КоммуналСервис в Мурино опять не вывезла мусор, жители пишут губернатору")
    assert m.neighbor and m.candidate


def test_old_street_names(mt):
    m = mt.match("Буденного 23к3 — снова прорыв трубы в подвале")
    assert "civilization" in m.companies()


LAMPO_REAL = ("Информация про мой дом. Прошу максимальный репост\n[репост] Жители жилого комплекса Лампо (г.Мурино) записали "
              "видеообращение представителям органов государственной власти, правоохранительным и надзорным службам, а также, "
              "губернатору области Александру Юрьевичу Дрозденко - о катастрофической ситуации по обслуживанию дома управляющей "
              "компанией «Возрождение Северо-Запада» и о затоплении части помещений. Сегодня все недовольны работой своих "
              "управляющих компаний, но видео ЖК \"Лампо\" пробрало даже видавших многое админов группы. Просьба всем жителям "
              "Мурино помочь своим соседям в ЖК \"Лампо\" репостами, комментами, лайками, выделением гиперссылками губернатора, "
              "органов власти. Размещайте новость в группах своих домов. @gilnadzor_lenoblast @drozdenko_a "
              "@lenobl_prokuratura @sledcomru @rospotrebnadzor.official")


def test_real_lampo_post_2026_07(mt):
    """Реальный пост 20–21.07.2026 (репост со стены инфлюенсера): должен быть срочным кандидатом."""
    m = mt.match(LAMPO_REAL)
    assert "vozrozhdenie" in m.companies() and "murino" in m.clusters and m.cta and m.candidate and m.urgency == 1
    assert {"gov_lo", "gzhn_lo", "prosecutor", "sk", "rpn"} <= set(m.addressees), m.addressees


def test_floor_is_not_uk_etazhi(mt):
    """Пилот 24.09: «детонация газа на 5 этаже» попала в отчёт как «УК Этажи»."""
    m = mt.match("Информация о детонации газа на 5 этаже многоквартирного дома по адресу: Товарищеский проспект, "
                 "дом 6, корпус 6. Обрушение межкомнатной перегородки.", source_region="SPB")
    assert "etazhi" not in m.companies()
    assert "etazhi" in mt.match("УК «Этажи» опять не убрала двор", source_region="LO").companies()


def test_namesake_from_other_region_is_not_neighbor(mt):
    """Пилот 24.09: «ООО Коммуналсервис» из Удомли (Тверская обл.) в отчёте как наш сосед."""
    m = mt.match("В Удомле органы прокуратуры выявили нарушения в деятельности управляющей компании "
                 "ООО «Коммуналсервис» по проспекту Курчатова")
    assert not m.neighbor and not m.candidate
    assert mt.match("УК КоммуналСервис в Мурино опять не вывезла мусор").neighbor


def test_channel_signature_is_not_district():
    from monitor.pipeline import strip_signature

    t = "Прокуратура нашла нарушения на свалке в Невском районе\n\n🌇  Жизнь в Кудрово  ( ВК | Max )\nЕсть новости?"
    assert "Кудрово" not in strip_signature(t, "Жизнь в Кудрово")
    assert "Кудрово" in strip_signature("В Кудрово третий день нет воды", "Жизнь в Кудрово")


def test_source_without_region_does_not_crash(mt):
    """Пилот 24.09: у части источников в базе region = NULL → падение при запуске."""
    assert mt.match("УК Этажи опять не вывезла мусор", source_category=None, source_region=None) is not None
