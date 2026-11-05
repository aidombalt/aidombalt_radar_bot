"""Сквозной тест «как в бою», без GPU и без внешней сети:
сервер Радара (GUI+мост) + заглушка LLM + Colab-воркер (тот же код, что в ноутбуке) → приём публикаций →
разбор «большим мозгом» → алерты → отчёт с текстом от Colab.  Запуск: python tools/e2e.py"""
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="radar_e2e_"))
ENV = {**os.environ, "HOST": "127.0.0.1", "MAX_BOT_TOKEN": "", "MAX_ADMIN_CHAT_ID": "", "RADAR_DATA_DIR": str(TMP), "BRIDGE_TOKEN": "e2e-token", "GUI_PASSWORD": "", "TG_BOT_TOKEN": "",
       "TG_ADMINS": "111", "VK_SERVICE_TOKEN": "", "SMTP_HOST": "", "LOG_LEVEL": "INFO", "PYTHONUNBUFFERED": "1"}
H = {"Authorization": "Bearer e2e-token"}
URL = "http://127.0.0.1:18080"
procs = []


def start(args, log):
    p = subprocess.Popen([sys.executable, *args], cwd=ROOT, env=ENV, stdout=open(TMP / log, "w"), stderr=subprocess.STDOUT)
    procs.append(p)
    return p


def wait(url, t=60):
    for _ in range(t * 2):
        try:
            if httpx.get(url, timeout=2).status_code < 500:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise SystemExit(f"не поднялся {url}")


RECENT = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")   # «свежий» пост


def db():
    c = sqlite3.connect(TMP / "radar.sqlite3")
    c.row_factory = sqlite3.Row
    return c


try:
    start(["tools/fake_llm.py"], "llm.log")
    start(["run.py", "--port", "18080"], "server.log")
    wait("http://127.0.0.1:18000/v1/models")
    wait(f"{URL}/health")
    print("✓ сервер и заглушка LLM подняты:", httpx.get(f"{URL}/health").json())

    assert httpx.post(f"{URL}/api/brain/heartbeat", json={}, headers={"Authorization": "Bearer WRONG"}).status_code == 401
    print("✓ мост отвергает неверный токен")

    start(["colab/brain_worker.py", "--server", URL, "--token", "e2e-token", "--llm-url", "http://127.0.0.1:18000",
           "--llm-kind", "openai", "--preset", "e2e-fake"], "worker.log")
    posts = [
        ("Александр Юрьевич! ЖК Виктория, Мурино, Авиаторов Балтики 9к1 — лифт стоит вторую неделю, УК Балтийский Дом "
         "не отвечает. Сколько можно?! Пишите все губернатору!", 12400),
        ("Жители ЖК «Лампо» пишут губернатору: УК «Возрождение Северо-Запада» не убирает двор, помогите", 800),
        ("Вчера в театре «Балтийский дом» была премьера спектакля", 50000),
        ("Спасибо УК Приморский Дом, быстро починили домофон в ЖК Виктория", 30),
    ]
    items = [{"platform": "tg", "external_id": f"tg:tmurino/{900 + i}", "url": f"https://t.me/tmurino/{900 + i}",
              "author_id": "tg:tmurino", "author_name": "Горячие новости Мурино", "author_followers": f,
              "text": t, "published_at": RECENT, "views": 1000} for i, (t, f) in enumerate(posts)]
    r = httpx.post(f"{URL}/api/brain/ingest", headers=H, json={"source_key": "tg_channel:tmurino", "items": items}).json()
    print("✓ приём из Colab:", r)
    assert r["new"] == 4 and r["cand"] == 3, r   # театр — не кандидат

    for _ in range(90):
        rows = db().execute("SELECT external_id, status, analysis_level, priority, summary FROM items "
                            "WHERE candidate=1").fetchall()
        if rows and all(x["status"] == "analyzed" for x in rows):
            break
        time.sleep(1)
    for x in rows:
        print(f"   {x['external_id']}: {x['priority']} [{x['analysis_level']}] {x['summary'][:70]}")
    assert all(x["analysis_level"] == "colab" for x in rows), "разобрал не Colab"
    pr = {x["external_id"]: x["priority"] for x in rows}
    assert pr["tg:tmurino/900"] == "P1" and pr["tg:tmurino/901"] in ("P2", "P1"), pr
    print("✓ Colab-мозг разобрал очередь, приоритеты верные")

    ob = db().execute("SELECT channel, status, COUNT(*) n FROM outbox GROUP BY channel, status").fetchall()
    print("✓ исходящие:", [dict(x) for x in ob])
    assert any(x["channel"] == "telegram" for x in ob)

    g = httpx.get(f"{URL}/", timeout=10)
    assert g.status_code == 200 and "nicegui" in g.text.lower()
    for route in ("/settings", "/events", "/reports"):
        page = httpx.get(f"{URL}{route}", timeout=10)
        assert page.status_code == 200 and "nicegui" in page.text.lower(), route
    print("✓ GUI отвечает (дашборд, настройки, журнал, отчёты)")

    rep = subprocess.run([sys.executable, "run.py", "--report", "2"], cwd=ROOT, env=ENV, capture_output=True, text=True,
                         timeout=400)
    last = [ln for ln in rep.stdout.splitlines() if ln.startswith("{")]
    print("✓ отчёт:", last[-1] if last else rep.stdout[-500:] + rep.stderr[-800:])
    res = eval(last[-1]) if last else {}
    assert res.get("narrative_by") == "colab", "текст отчёта должен написать Colab через очередь заданий"
    print("✓ текст аналитической записки подготовил Colab (очередь заданий между процессами)")
    print("\nВСЕ ПРОВЕРКИ ПРОЙДЕНЫ ·", TMP)
    print(json.dumps({"xlsx": res["xlsx"], "docx": res["docx"]}, ensure_ascii=False))
finally:
    for p in procs:
        p.terminate()
