"""Colab-воркер «мозга». Запускается из brain_worker.ipynb (или на любой машине с GPU — тот же код).

Что делает:
  1) поднимает open-source модель (vLLM или Ollama) под выданную GPU — с автоматическим откатом на меньшую;
  2) забирает у сервера очередь публикаций, разбирает агентом (core.py с сервера), возвращает результат;
  3) собирает «зарубежные» источники: Telegram-каналы (t.me/s), Google News, заблокированные в РФ СМИ;
  4) ретранслирует алерты в Telegram, если сервер из РФ не может до него достучаться;
  5) сторожит сервер: если он молчит >15 мин — пишет админам в Telegram напрямую.
Все соединения — исходящие из Colab на сервер. Никаких туннелей.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time
import types
from pathlib import Path

import httpx

# ------------------------------------------------------------------ пресеты моделей (сентябрь 2026)
PRESETS = [
    {"name": "Qwen3.8-27B FP8 · vLLM", "min_vram": 70, "engine": "vllm", "model": "Qwen/Qwen3.8-27B-FP8",
     "max_len": 32768, "gpu_util": 0.92, "vision": True, "concurrency": 12,
     "vllm_args": ["--kv-cache-dtype", "fp8", "--reasoning-parser", "qwen3"],
     "extra": {"chat_template_kwargs": {"enable_thinking": False}}},
    {"name": "Qwen3.8-27B FP8 · vLLM (40 ГБ)", "min_vram": 38, "engine": "vllm", "model": "Qwen/Qwen3.8-27B-FP8",
     "max_len": 12288, "gpu_util": 0.95, "vision": False, "concurrency": 6,
     "vllm_args": ["--kv-cache-dtype", "fp8", "--reasoning-parser", "qwen3", "--limit-mm-per-prompt", '{"image":0,"video":0}'],
     "extra": {"chat_template_kwargs": {"enable_thinking": False}}},
    {"name": "Qwen3.8-27B Q4_K_M · Ollama", "min_vram": 22, "engine": "ollama",
     "model": "hf.co/ggml-org/Qwen3.8-27B-GGUF:Q4_K_M", "concurrency": 2},
    {"name": "Gemma 4 12B QAT · Ollama", "min_vram": 14, "engine": "ollama",
     "model": "hf.co/google/gemma-4-12B-it-qat-q4_0-gguf", "concurrency": 2},
    {"name": "Gemma 4 E4B QAT · Ollama (CPU)", "min_vram": 0, "engine": "ollama",
     "model": "hf.co/google/gemma-4-E4B-it-qat-q4_0-gguf", "concurrency": 1},
]
LOGDIR = "/content" if os.path.isdir("/content") else "/tmp"


def compute_cap() -> float:
    """8.0 = A100 (Ampere), 8.9 = L4 (Ada), 9.0 = H100. FP8-кэш и часть ядер нужны ≥ 8.9."""
    r = sh("nvidia-smi --query-gpu=compute_cap --format=csv,noheader")
    try:
        return float(r.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        return 0.0


def gpu_info() -> tuple[str, float]:
    if not shutil.which("nvidia-smi"):
        return "CPU", 0.0
    out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True).stdout.strip().splitlines()
    if not out:
        return "CPU", 0.0
    name, mem = out[0].split(",")
    return name.strip().replace("NVIDIA ", ""), float(mem) / 1024


def cu_rate(gpu: str) -> float:
    """Примерный расход compute units в час (H100 — оценка)."""
    g = gpu.upper()
    if "H100" in g:
        return 11.0
    if "A100" in g:
        return 7.52 if "80" in g else 5.40
    if "L4" in g:
        return 1.71
    if "T4" in g:
        return 1.19
    return 0.1 if g == "CPU" else 2.0


def pick_presets(vram: float, force: str | None = None) -> list[dict]:
    if force:
        return [p for p in PRESETS if force.lower() in p["name"].lower()] or PRESETS
    return [p for p in PRESETS if vram >= p["min_vram"]]


# ------------------------------------------------------------------ движки
def sh(cmd: str, log: str | None = None, bg: bool = False):
    if bg:
        f = open(log or os.devnull, "a")
        return subprocess.Popen(cmd, shell=True, stdout=f, stderr=subprocess.STDOUT)
    return subprocess.run(cmd, shell=True, check=False, capture_output=True, text=True)


async def wait_http(url: str, timeout: float, proc=None, log: str | None = None) -> bool:
    """Ждём, пока движок ответит. Если процесс умер — выходим сразу (не жжём CU впустую)."""
    t0, last_note = time.time(), 0.0
    async with httpx.AsyncClient() as c:
        while time.time() - t0 < timeout:
            try:
                if (await c.get(url, timeout=5)).status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            if proc is not None and proc.poll() is not None:
                print(f"❌ движок завершился с кодом {proc.returncode}")
                return False
            if log and time.time() - last_note > 60:
                last_note = time.time()
                tail = sh(f"tail -n 1 {log}").stdout.strip()[-160:]
                print(f"   … {int(time.time() - t0)} с: {tail}")
            await asyncio.sleep(5)
    return False


BROKEN: set[str] = set()     # движки, которые не удалось установить в этой сессии — не пробуем повторно
VLLM_ENV = f"{LOGDIR}/vllm-env"


def ensure_build_tools() -> None:
    """FlashInfer компилирует ядра «на лету» при первом запуске — нужен ninja (и nvcc из /usr/local/cuda)."""
    if not shutil.which("ninja"):
        sh("apt-get -qq update >/dev/null 2>&1; apt-get -qq install -y ninja-build >/dev/null 2>&1")
    if not shutil.which("ninja"):
        sh(f"{sys.executable} -m pip install -q ninja")


def vllm_env_prefix() -> str:
    cuda = "/usr/local/cuda"
    return (f"PATH={VLLM_ENV}/bin:{cuda}/bin:$PATH CUDA_HOME={cuda} "
            if os.path.isdir(cuda) else f"PATH={VLLM_ENV}/bin:$PATH ")


def install_vllm() -> bool:
    """vLLM — в ОТДЕЛЬНОМ окружении (uv): иначе он обновляет PyTorch Colab и ломает torchaudio/transformers."""
    py = f"{VLLM_ENV}/bin/python"
    if os.path.exists(py) and sh(f"{py} -c 'import vllm'").returncode == 0:
        return True
    print("⏳ Ставлю vLLM в отдельное окружение (3–6 мин)…")
    steps = [f"{sys.executable} -m pip install -q uv",
             f"uv venv {VLLM_ENV} --python 3.12 --seed -q",
             f"uv pip install --python {py} 'vllm>=0.30' ninja --torch-backend=auto -q"]
    for cmd in steps:
        r = sh(cmd)
        if r.returncode != 0:
            print("❌ vLLM не установился:", (r.stderr or r.stdout)[-800:])
            return False
    return sh(f"{py} -c 'import vllm'").returncode == 0


def install_ollama() -> bool:
    if shutil.which("ollama"):
        return True
    print("⏳ Ставлю Ollama…")
    sh("apt-get -qq update && apt-get -qq install -y zstd pciutils lshw")   # установщику нужен zstd
    r = sh("curl -fsSL https://ollama.com/install.sh | sh")
    if not shutil.which("ollama"):
        print("❌ Ollama не установилась:", (r.stderr or r.stdout)[-800:])
        return False
    return True


def root_cause(log: str) -> str:
    """Настоящая ошибка — в строках EngineCore, а не в хвосте трассировки APIServer."""
    r = sh(f"grep -iE 'error|exception|not support|out of memory|assert|unsupported' {log} "
           f"| grep -v 'APIServer' | grep -v 'deprecat' | tail -n 15")
    return r.stdout.strip() or sh(f"tail -n 25 {log}").stdout


async def start_engine(p: dict) -> tuple[str, str] | None:
    """Возвращает (base_url, kind) или None, если пресет не взлетел."""
    if p["engine"] in BROKEN:
        return None
    if p["engine"] == "vllm":
        if not install_vllm():
            BROKEN.add("vllm")
            return None
        ensure_build_tools()
        base = list(p.get("vllm_args", []))
        if compute_cap() < 8.9 and "--kv-cache-dtype" in base:      # A100: FP8 KV-кэш не поддерживается
            i = base.index("--kv-cache-dtype")
            del base[i:i + 2]
        log = f"{LOGDIR}/vllm.log"
        for attempt, extra in enumerate(([], ["--enforce-eager"]), 1):   # 2-я попытка: без компиляции/CUDA-графов
            args = " ".join(f"'{a}'" for a in base + extra)
            cmd = (f"{vllm_env_prefix()}{VLLM_ENV}/bin/python -m vllm.entrypoints.openai.api_server --model {p['model']} --host 127.0.0.1 "
                   f"--port 8000 --max-model-len {p['max_len']} --gpu-memory-utilization {p['gpu_util']} "
                   f"--served-model-name radar {args}")
            open(log, "w").close()
            print(f"⏳ Запускаю vLLM ({attempt}/2{', без компиляции' if extra else ''}):", p["model"])
            proc = sh(cmd, log, bg=True)
            if await wait_http("http://127.0.0.1:8000/v1/models", 1500, proc, log):
                return "http://127.0.0.1:8000", "openai"
            sh("pkill -f vllm.entrypoints")
            cause = root_cause(log)
            print("❌ vLLM не поднялся. Причина из лога:\n" + cause)
            if "out of memory" in cause.lower():
                return None                    # пусть попробует следующий пресет (меньше контекст)
        BROKEN.add("vllm")                     # не память — другие пресеты vLLM упадут так же, сразу к Ollama
        return None
    if not install_ollama():
        BROKEN.add("ollama")
        return None
    if not await wait_http("http://127.0.0.1:11434/api/tags", 3):
        sh("OLLAMA_HOST=127.0.0.1:11434 OLLAMA_NUM_PARALLEL=2 ollama serve", f"{LOGDIR}/ollama.log", bg=True)
        await wait_http("http://127.0.0.1:11434/api/tags", 120)
    print("⏳ Загружаю модель", p["model"])
    r = sh(f"ollama pull {p['model']}")
    if r.returncode != 0:
        print("❌ ollama pull:", (r.stdout or "")[-400:], (r.stderr or "")[-400:])
        return None
    return "http://127.0.0.1:11434", "ollama"


# ------------------------------------------------------------------ воркер
class Worker:
    def __init__(self, server: str, token: str, base_url: str, kind: str, preset: dict, gpu: str, core, portable):
        self.server, self.token = server.rstrip("/"), token
        self.core, self.portable = core, portable
        model = "radar" if kind == "openai" else preset["model"]
        self.llm = core.LLM(base_url, model, kind=kind, timeout=180, extra=preset.get("extra"),
                            vision=preset.get("vision", False), name=f"colab:{preset['name']}")
        self.preset, self.gpu = preset, gpu
        self.wid = f"colab-{os.environ.get('HOSTNAME', 'x')[:12]}"
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=15),
                                      headers={"Authorization": f"Bearer {token}"})
        self.ext = httpx.AsyncClient(timeout=30, follow_redirects=True)
        self.t0, self.done, self.errors = time.time(), 0, 0
        self.relay_cfg: dict = {}
        self.server_ok_at = time.time()
        self.company, self.max_rounds = "ГК «Балтийский Дом»", 2
        self._last_collect: dict[str, float] = {}

    async def api(self, method: str, path: str, **kw):
        r = await self.http.request(method, f"{self.server}{path}", **kw)
        r.raise_for_status()
        self.server_ok_at = time.time()
        return r.json()

    async def tool(self, name: str, query: str, item: dict) -> str:
        j = await self.api("POST", "/api/brain/tool", json={"name": name, "query": query, "item_id": item.get("id")})
        return j.get("result", "")

    # ---------------- циклы
    async def heartbeat_loop(self):
        while True:
            try:
                j = await self.api("POST", "/api/brain/heartbeat", json={
                    "worker_id": self.wid, "gpu": self.gpu, "model": self.preset["name"], "engine": self.preset["engine"],
                    "cu_per_hour": cu_rate(self.gpu), "uptime_min": int((time.time() - self.t0) / 60),
                    "done": self.done, "errors": self.errors, "want_relay": True, "core": self.core.CORE_VERSION})
                self.relay_cfg = j.get("relay") or self.relay_cfg
                self.company, self.max_rounds = j.get("company", self.company), j.get("max_rounds", 2)
            except Exception as e:
                print("⚠️ heartbeat:", e)
            await asyncio.sleep(30)

    async def analyze_loop(self):
        conc = self.preset.get("concurrency", 4)
        while True:
            try:
                pk = (await self.api("POST", "/api/brain/lease", json={"worker_id": self.wid, "limit": conc * 2}))["packages"]
            except Exception as e:
                print("⚠️ lease:", e)
                await asyncio.sleep(10)
                continue
            if not pk:
                await asyncio.sleep(3)
                continue
            res = await self.core.run_batch(pk, self.llm, self.tool, concurrency=conc, max_rounds=self.max_rounds,
                                            company=self.company)
            self.done += sum(1 for r in res if r["ok"])
            self.errors += sum(1 for r in res if not r["ok"])
            try:
                await self.api("POST", "/api/brain/result", json={"worker_id": self.wid, "results": res})
                pr = [r["analysis"]["priority"] for r in res if r["ok"]]
                print(f"🧠 {time.strftime('%H:%M:%S')} разобрано {len(pr)}: {', '.join(pr)}  (всего {self.done})")
            except Exception as e:
                print("⚠️ result:", e)

    async def collect_loop(self):
        while True:
            try:
                plan = await self.api("GET", "/api/brain/collect")
                iv = plan.get("intervals", {})
                for s in plan.get("sources", []):
                    every = 60 * iv.get(s["tier"], 15)
                    if time.time() - self._last_collect.get(s["key"], 0) < every:
                        continue
                    self._last_collect[s["key"]] = time.time()
                    await self.collect_one(s)
            except Exception as e:
                print("⚠️ collect plan:", e)
            await asyncio.sleep(30)

    async def collect_one(self, s: dict):
        P = self.portable
        body = {"source_key": s["key"], "items": [], "cursors": {}}
        try:
            if s["kind"] == "tg_channel":
                items, info = await P.fetch_tg_channel(self.ext, s["handle"], s.get("since_id", 0))
                for i in items:
                    if i.get("forwarded_from"):
                        i["text"] = f"[переслано из: {i['forwarded_from']}] {i['text']}"
                body["items"] = items
                body["members"] = info.get("subscribers", 0)
                if items:
                    body["cursors"][f"tg:{s['handle'].lstrip('@')}"] = max(i["msg_id"] for i in items)
            elif s["kind"] == "gnews":
                r = await self.ext.get(P.gnews_url(s["handle"]))
                r.raise_for_status()
                body["items"] = P.feed_to_items(P.parse_feed(r.content), s["key"], platform="gnews")
            elif s["kind"] == "rss":
                meta = s.get("meta") or {}
                feed = meta.get("feed") or await P.discover_rss(self.ext, s["handle"]) or s["handle"]
                r = await self.ext.get(feed)
                r.raise_for_status()
                body["items"] = P.feed_to_items(P.parse_feed(r.content), s["key"])
                if feed != meta.get("feed"):
                    body["meta_feed"] = feed
            else:
                return
        except Exception as e:
            body = {"source_key": s["key"], "error": f"{type(e).__name__}: {e}"}
        try:
            j = await self.api("POST", "/api/brain/ingest", json=body)
            if j.get("cand"):
                print(f"📡 {s['key']}: новых {j.get('new')}, кандидатов {j.get('cand')}")
        except Exception as e:
            print("⚠️ ingest:", e)

    async def relay_loop(self):
        while True:
            await asyncio.sleep(20)
            tok = self.relay_cfg.get("bot_token")
            if not tok:
                continue
            try:
                msgs = (await self.api("POST", "/api/brain/relay/lease", json={"worker_id": self.wid}))["messages"]
            except Exception:
                continue
            ok, fail = [], []
            for m in msgs:
                try:
                    r = await self.ext.post(f"https://api.telegram.org/bot{tok}/sendMessage", json=m["payload"])
                    (ok if r.json().get("ok") else fail).append(m["id"])
                except Exception:
                    fail.append(m["id"])
            if msgs:
                await self.api("POST", "/api/brain/relay/ack", json={"ok": ok, "fail": fail})
                print(f"📨 ретранслировано в Telegram: {len(ok)} (ошибок {len(fail)})")

    async def jobs_loop(self):
        while True:
            await asyncio.sleep(15)
            try:
                jobs = (await self.api("POST", "/api/brain/jobs"))["jobs"]
                for j in jobs:
                    text = await self.core.narrative(self.llm, j["facts"], self.company)
                    await self.api("POST", "/api/brain/job_result", json={"id": j["id"], "text": text})
                    print("📝 текст отчёта подготовлен")
            except Exception as e:
                print("⚠️ jobs:", e)

    async def server_watchdog(self):
        warned = False
        while True:
            await asyncio.sleep(60)
            silent = (time.time() - self.server_ok_at) / 60
            tok, admins = self.relay_cfg.get("bot_token"), self.relay_cfg.get("admins", [])
            if silent > 15 and not warned and tok:
                for a in admins:
                    try:
                        await self.ext.post(f"https://api.telegram.org/bot{tok}/sendMessage", json={
                            "chat_id": a, "text": f"⛔️ Сервер Радара не отвечает Colab-мозгу уже {silent:.0f} мин. "
                                                  "Мониторинг может не работать — проверьте сервер."})
                    except Exception:
                        pass
                warned = True
            if silent < 2:
                warned = False

    async def run(self, max_hours: float = 0):
        rate = cu_rate(self.gpu)
        print(f"✅ Мозг на связи: {self.preset['name']} · GPU {self.gpu} · ≈{rate} CU/ч"
              + (f" · автостоп через {max_hours:g} ч (≈{rate * max_hours:.0f} CU)" if max_hours else ""))
        loops = asyncio.gather(self.heartbeat_loop(), self.analyze_loop(), self.collect_loop(), self.relay_loop(),
                               self.jobs_loop(), self.server_watchdog())
        if not max_hours:
            return await loops
        try:
            await asyncio.wait_for(loops, timeout=max_hours * 3600)
        except asyncio.TimeoutError:   # незабранные задания вернутся в очередь по истечении аренды
            print(f"⏹ {max_hours:g} ч отработано — выключаюсь, чтобы не жечь CU. Радар продолжит на локальной модели.")


# ------------------------------------------------------------------ автономный самотест (без сервера)
SAMPLES = [  # (площадка, категория источника, подписчики, ожидание, текст)
    ("vk_comment", "governor", 12400, "P1", "Александр Юрьевич! ЖК Виктория, Мурино, Авиаторов Балтики 9к1 — лифт стоит "
     "вторую неделю, УК не отвечает. Сколько можно?! Пишите все губернатору!"),
    ("tg", "public", 24100, "P1/P2", "Жители ЖК «Лампо» пишут губернатору: УК «Возрождение Северо-Запада» не убирает двор. "
     "В комментариях соседи с Екатерининской жалуются на своих управляющих."),
    ("vk_comment", "governor", 150, "P1", "Александр Дмитриевич, Буденного 23к3 — прорыв трубы в подвале, вода стоит, "
     "запах. Примите меры!"),
    ("vk_comment", "public", 40, "P2", "ЖК Прогресс, Европейский 13 к4 — третий день нет горячей воды, УК молчит!"),
    ("vk_comment", "public", 30, "P3/P4", "Спасибо УК Приморский Дом, быстро починили домофон в ЖК Цвета Радуги!"),
    ("rss", "media", 0, "P4", "Театр «Балтийский дом» открыл новый сезон премьерой спектакля"),
    # 23.10: СК о доме «на проспекте Обуховской Обороны» без номера — наш там один дом из сотен: не наш объект
    ("tg", "official", 68300, "P3/P4", "Председателю СК России будет представлен доклад о нарушении жилищных прав граждан в "
     "Санкт-Петербурге. Жители многоквартирного дома на проспекте Обуховской Обороны несколько лет сообщают об аварийном "
     "состоянии здания: разрушаются потолки, протечки. Жильцы обращались в управляющую компанию, ремонт не выполнен. "
     "Возбуждено уголовное дело, Бастрыкин поручил представить доклад."),
    # 21–23.10: наш ЖК и адрес названы, но это хулиганство (зона полиции), претензии к УК нет — не «ответить от лица УК»
    ("vk_post", "public", 76223, "P3/P4", "Уважаемые жители ЖК \"Виктория\", прошу вашей помощи. Мои родители, пенсионеры, "
     "живут по адресу: Авиаторов Балтики, 9к2. Уже около года раз в две недели подростки забрасывают их входную дверь "
     "яйцами и с силой бьют по ней ногами, сразу убегают. Пришлось установить камеру. Судя по записи, страдают и другие жители."),
    ("vk_post", "public", 5000, "P4", "Агентство недвижимости Этажи: квартиры в Мурино в ипотеку от 6%"),
    ("vk_post", "public", 6141, "P1", "Информация про мой дом. Прошу максимальный репост\n[репост] Жители жилого "
     "комплекса Лампо (г.Мурино) записали видеообращение представителям органов государственной власти, правоохранительным "
     "и надзорным службам, а также губернатору области Александру Юрьевичу Дрозденко — о катастрофической ситуации по "
     "обслуживанию дома управляющей компанией «Возрождение Северо-Запада» и о затоплении части помещений. Просьба всем "
     "жителям Мурино помочь соседям репостами, комментами, лайками, выделением гиперссылками губернатора. Размещайте "
     "новость в группах своих домов. @gilnadzor_lenoblast @drozdenko_a @lenobl_prokuratura @sledcomru"),
]


def final_priority(a: dict, mt, followers: int, cat: str) -> str:
    """Боевой приоритет: «два ключа» — модель и детерминированные правила (monitor/risk.py), берётся строжайший."""
    if a.get("priority") == "ERR":
        return "ERR"
    try:
        from monitor import risk
        from monitor.settings import load_settings
        cfg = load_settings().risk
        sc, _ = risk.score({"author_followers": followers}, mt.to_json() if mt else {}, a, cfg, 0.0, 0, cat)
        return risk.combine(a.get("priority", "P3"), risk.priority_from_score(sc, cfg), a.get("relevant", False), "llm")[0]
    except Exception as e:  # без кода сервера — только мнение модели
        print("   (итог по правилам недоступен:", e, ")")
        return a.get("priority", "P3")


async def selftest(code_dir: str, force_preset: str | None = None, llm_url: str | None = None, llm_kind: str = "openai"):
    """Проверка «мозга» без сервера: поднимает модель (или берёт готовую по llm_url) и разбирает 8 эталонов."""
    sys.path.insert(0, code_dir)
    from monitor.brain import core
    from monitor.entities import load_registry
    reg = load_registry()
    try:
        from monitor.matcher import Matcher
        matcher = Matcher(reg)
    except Exception as e:  # rapidfuzz/pymorphy3 не установлены — без подсказок матчера
        print("⚠️ матчер недоступен:", e)
        matcher = None
    gpu, vram = gpu_info()
    print(f"GPU: {gpu} ({vram:.0f} ГБ)" + ("" if llm_url else f" · ≈{cu_rate(gpu)} CU/ч"))
    presets = [{"name": "external", "engine": llm_kind, "model": force_preset or "radar"}] if llm_url else pick_presets(vram, force_preset)
    for p in presets:
        t_start = time.time()
        started = (llm_url, llm_kind) if llm_url else await start_engine(p)
        if not started:
            continue
        url, kind = started
        llm = core.LLM(url, "radar" if kind == "openai" else p["model"], kind=kind, timeout=300, extra=p.get("extra"),
                       name=p["name"])
        print(f"\n✅ {p['name']} запущена за {time.time() - t_start:.0f} с. Разбор эталонов "
              "(ИИ — мнение модели, ИТОГ — как в бою: строже из ИИ и правил):\n")
        ok, lat, hits = 0, [], 0
        for i, (plat, cat, fol, expect, text) in enumerate(SAMPLES, 1):
            mt = matcher.match(text, source_category=cat) if matcher else None
            hint = matcher.hint(mt) if mt else ""
            pkg = {"item": {"id": i, "platform": plat, "text": text, "author_followers": fol, "source_category": cat,
                            "source_region": "", "published_at": "2026-09-23T10:00:00Z", "author_name": "тест"},
                   "catalog": reg.catalog_text(), "hint": hint}
            t0 = time.time()
            try:
                a = await core.analyze(pkg, llm, None, max_rounds=0)
                ok += 1
            except Exception as e:
                a = {"priority": "ERR", "relation": "", "summary": str(e)[:120]}
            lat.append(time.time() - t0)
            final = final_priority(a, mt, fol, cat)
            hits += final in expect.split("/")
            mark = "✓" if final in expect.split("/") else "✗"
            print(f"{mark} ждём {expect:<5} → ИИ {a['priority']:<3} ИТОГ {final:<3} {a.get('relation', ''):<8} {lat[-1]:5.1f} с | "
                  f"{a.get('summary', '')[:90]}")
        print(f"\nИтог: валидных ответов {ok}/{len(SAMPLES)}, попаданий {hits}/{len(SAMPLES)}, "
              f"среднее время {sum(lat) / len(lat):.1f} с на публикацию.")
        print("Скопируйте этот вывод целиком и отправьте в чат.")
        return
    print("❌ Ни одна модель не запустилась — пришлите вывод выше.")


async def load_code(server: str, token: str):
    """core.py и portable.py берём с сервера — логика анализа всегда совпадает с серверной."""
    async with httpx.AsyncClient(timeout=60, headers={"Authorization": f"Bearer {token}"}) as c:
        r = await c.get(f"{server.rstrip('/')}/api/brain/code")
        r.raise_for_status()
        code = r.json()
    mods = []
    for name in ("core", "portable"):
        m = types.ModuleType(f"radar_{name}")
        exec(compile(code[name], f"radar_{name}.py", "exec"), m.__dict__)
        sys.modules[f"radar_{name}"] = m
        mods.append(m)
    Path("/tmp/radar_core.py").write_text(code["core"])
    return mods


async def main(server: str, token: str, force_preset: str | None = None, base_url: str | None = None,
               kind: str | None = None, max_hours: float = 0):
    core, portable = await load_code(server, token)
    gpu, vram = gpu_info()
    print(f"GPU: {gpu} ({vram:.0f} ГБ) · расход ≈ {cu_rate(gpu)} CU/ч → 100 CU хватит на ≈ {100 / cu_rate(gpu):.0f} ч")
    if base_url:     # уже поднятый сервер модели (локальный запуск/отладка)
        preset = {"name": force_preset or "external", "engine": kind or "openai", "model": force_preset or "radar", "concurrency": 4}
        return await Worker(server, token, base_url, kind or "openai", preset, gpu, core, portable).run(max_hours)
    for p in pick_presets(vram, force_preset):
        started = await start_engine(p)
        if not started:
            continue
        url, k = started
        w = Worker(server, token, url, k, p, gpu, core, portable)
        try:   # пробный запрос: если модель не отвечает JSON — следующий пресет
            await w.llm.chat_json([{"role": "user", "content": 'Ответь JSON {"ok": true}'}],
                                  {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]})
        except Exception as e:
            print(f"❌ {p['name']} не прошёл проверку: {e}")
            continue
        return await w.run(max_hours)
    raise SystemExit("Ни одна модель не запустилась — смените тип GPU или посмотрите логи vllm.log / ollama.log")


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default=os.environ.get("RADAR_URL"))
    ap.add_argument("--token", default=os.environ.get("RADAR_TOKEN"))
    ap.add_argument("--preset")
    ap.add_argument("--llm-url", help="готовый сервер модели (OpenAI-совместимый или Ollama)")
    ap.add_argument("--llm-kind", default="openai")
    ap.add_argument("--max-hours", type=float, default=0, help="автостоп через N часов (0 — без лимита)")
    a = ap.parse_args()
    asyncio.run(main(a.server, a.token, a.preset, a.llm_url, a.llm_kind, a.max_hours))
