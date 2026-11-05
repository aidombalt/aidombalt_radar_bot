from monitor.brain.core import extract_json, normalize_analysis
from monitor.collectors import portable
from tests.conftest import FIX


def test_tg_page():
    items, info = portable.parse_tg_page((FIX / "tg_tmurino.html").read_text("utf-8"), "tmurino")
    assert info["subscribers"] == 24100 and info["title"].startswith("Горячие")
    assert [i["msg_id"] for i in items] == [12070, 12071]
    first = items[0]
    assert "Лампо" in first["text"] and "\n" in first["text"]
    assert first["views"] == 3400 and first["published_at"] == "2026-07-18T09:12:44Z"
    assert first["media"] == ["https://cdn4.cdn-telegram.org/file/abc123.jpg"]
    assert items[1]["forwarded_from"] == "Александр Дрозденко"


def test_rss():
    entries = portable.parse_feed((FIX / "rss_sample.xml").read_bytes())
    items = portable.feed_to_items(entries, "rss:fontanka")
    assert len(items) == 2 and items[0]["published_at"] == "2026-09-22T13:08:47Z"
    assert "Авиаторов Балтики" in items[0]["text"] and "<b>" not in items[0]["text"]


def test_extract_json_robust():
    assert extract_json('<think>хм</think>```json\n{"a": "x\\"}y"}\n```') == {"a": 'x"}y'}
    assert extract_json('Вот ответ: {"relevant": true, "n": {"k": 1}} спасибо') == {"relevant": True, "n": {"k": 1}}
    assert extract_json("мусор") is None


def test_normalize_contradiction():
    a = normalize_analysis({"relevant": False, "priority": "P1", "sentiment": "-1.6", "topics": ["лифт", "чушь"]})
    assert a["priority"] == "P3" and a["sentiment"] == -2 and a["topics"] == ["лифт"]
