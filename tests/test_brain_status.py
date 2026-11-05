"""Статус локальной модели и «зависшие в работе» элементы (28.09: после перезапуска «Локальная модель ⚪», «в работе 3»)."""
from datetime import timedelta

import httpx

from monitor.brain.core import LLM
from monitor.brain.router import BrainRouter, local_state
from monitor.util import iso, now_utc

MODEL = "hf.co/google/gemma-4-E4B-it-qat-q4_0-gguf"


def llm_with(handler) -> LLM:
    llm = LLM("http://127.0.0.1:11434", MODEL, kind="ollama")
    llm._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return llm


def tags(*names):
    return lambda req: httpx.Response(200, json={"models": [{"name": n} for n in names]})


async def test_check_model_present_or_missing():
    ok, note = await llm_with(tags("hf.co/google/gemma-4-e4b-it-qat-q4_0-gguf:latest")).check()
    assert ok and note == ""                                                   # регистр и :latest не важны
    ok, note = await llm_with(tags("gemma3:4b")).check()
    assert not ok and "нет" in note and "gemma3:4b" in note                    # Ollama жива, модели нет — это не «онлайн»

    def down(req):
        raise httpx.ConnectError("refused")
    ok, note = await llm_with(down).check()
    assert not ok and "Ollama не запущена" in note


def test_localhost_bypasses_proxy():
    assert LLM("http://127.0.0.1:11434", MODEL)._client._trust_env is False
    assert LLM("https://llm.example.org", MODEL)._client._trust_env is True


def _router(ctx, handler):
    ctx.settings.brain.local.enabled = True
    r = BrainRouter(ctx)
    r.local = llm_with(handler)
    return r


async def test_status_fresh_with_empty_queue(ctx):
    r = _router(ctx, tags(MODEL))
    assert local_state(ctx).startswith("⏳")                                    # до первой проверки — не «офлайн»
    await r.tick()                                                              # очередь пуста, но статус проверен
    assert ctx.brain.local_online and local_state(ctx).startswith("🟢")


async def test_one_glitch_does_not_drop_status(ctx):
    state = {"up": True}

    def flaky(req):
        if not state["up"]:
            raise httpx.ConnectError("refused")
        return tags(MODEL)(req)
    r = _router(ctx, flaky)
    assert await r.local_online(force=True)
    state["up"] = False
    assert await r.local_online(force=True)                                     # один сбой — ещё онлайн
    assert not await r.local_online(force=True)                                 # второй подряд — офлайн
    assert local_state(ctx).startswith("🔴")


async def test_notice_only_after_5_minutes_down(ctx):
    def down(req):
        raise httpx.ConnectError("refused")
    r = _router(ctx, down)
    await r.local_online(force=True)
    notices = lambda: ctx.db.scalar("SELECT COUNT(*) FROM outbox WHERE dedup_key LIKE 'adm:local:0%'", (), 0)
    assert notices() == 0                                                       # ноутбук только загрузился — молчим
    r._local_down_since -= 301
    await r.local_online(force=True)
    await r.local_online(force=True)
    assert notices() == 1                                                       # одно сообщение на весь перерыв
    r.local = llm_with(tags(MODEL))
    await r.local_online(force=True)
    assert ctx.db.scalar("SELECT COUNT(*) FROM outbox WHERE dedup_key LIKE 'adm:local:1%'", (), 0) == 1


async def test_stuck_leases_are_released(ctx):
    def add(owner, until):
        return ctx.db.x("INSERT INTO items(platform, external_id, source_key, url, text, published_at, collected_at, status, "
                        "lease_owner, lease_until, urgency, queued_at) VALUES('tg', ?, 'tg_channel:x', 'u', 't', ?, ?, "
                        "'leased', ?, ?, 0, ?)", (f"{owner}{until}", iso(), iso(), owner, until, iso()))
    mine = add("server", iso(now_utc() + timedelta(minutes=9)))                 # Радар перезапустили посреди разбора
    gone = add("colab-1", iso(now_utc() - timedelta(minutes=1)))                # Colab взял и пропал
    busy = add("colab-2", iso(now_utc() + timedelta(minutes=9)))                # Colab ещё работает
    r = _router(ctx, tags("other:1b"))
    r.local = None
    await r.tick()
    st = {i: ctx.db.scalar("SELECT status FROM items WHERE id=?", (i,)) for i in (mine, gone, busy)}
    assert st[busy] == "leased"
    assert st[mine] != "leased" and st[gone] != "leased"                        # вернулись в очередь и разобраны правилами
