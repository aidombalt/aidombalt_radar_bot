// Радар · релей на Cloudflare Workers (бесплатно, 100 000 запросов в сутки — нам нужно ~15 000).
// Зачем: из сети офиса закрыты Telegram (бот и t.me) и Google News, а Cloudflare открыт.
// Ноутбук ходит к этим трём сервисам через этот релей; ВК, госсайты и всё остальное — напрямую.
//
// Установка (10 минут, один раз):
//   1. dash.cloudflare.com → регистрация (почта) → Workers & Pages → Create → Worker → имя radar-relay → Deploy.
//   2. «Edit code» → удалить всё → вставить этот файл → в строке KEY придумать ключ (латиница и цифры, 24+ знака) → Deploy.
//   3. В .env ноутбука: RELAY_URL=https://radar-relay.<ваше-имя>.workers.dev/<тот же ключ>
//   4. Перезапустить Радар и проверить: start_radar.bat --netcheck
// Обновление: «Edit code» → вставить новую версию, СОХРАНИВ свою строку KEY → Deploy.
//
// Чужие без ключа получают 403; хосты — только из списка ALLOW (это не «открытый прокси»).
//
// v2: страницы t.me/s ужимаются здесь, на стороне Cloudflare, до «скелета» (только посты, дата, просмотры,
// фото) и только НОВЫЕ посты (заголовок X-Radar-After). Было 100–300 КБ — стало 2–15 КБ. Большие ответы
// через Cloudflare из РФ «замирают» на середине, маленькие проходят.
// v3: то же для лент Google News — без громоздких описаний, не больше 25 новостей.
// v4: в ответах на посты («Photo, Массовые отключения…» сверху) раньше бралась цитата вместо текста самого поста —
//     теперь текст поста и цитата идут отдельно. Обновить обязательно (Радар напомнит в боте, если версия старая).
// v5: /gn-resolve/<id> — ссылка Google News → адрес самой статьи (news.google.com в РФ закрыт, а в алерте нужен
//     оригинал). Google отвечает релею, потому что он за рубежом.

const KEY = "ЗАМЕНИТЕ_НА_СВОЙ_КЛЮЧ";
const VERSION = "5";
const ALLOW = ["api.telegram.org", "t.me", "telegram.me", "news.google.com", "hc-ping.com"];  // hc-ping — внешний сторож (по желанию)
const DROP = ["host", "cf-connecting-ip", "cf-ipcountry", "cf-ray", "cf-visitor", "x-forwarded-for",
              "x-forwarded-proto", "x-real-ip", "x-radar-after", "x-radar-limit"];

export default {
  async fetch(request) {
    const url = new URL(request.url);
    const [, key, host, ...rest] = url.pathname.split("/");
    if (KEY.startsWith("ЗАМЕНИТЕ") || key !== KEY) return new Response("forbidden", { status: 403 });
    if (host === "gn-resolve") {
      const id = rest[0] || "";
      if (!/^[A-Za-z0-9_-]{16,}$/.test(id)) return json({ error: "bad id" }, 400);
      try {
        return json(await gnResolve(id));
      } catch (e) {
        return json({ error: String(e).slice(0, 200) }, 502);
      }
    }
    if (!ALLOW.includes(host)) return new Response("host not allowed", { status: 403 });

    const after = parseInt(request.headers.get("x-radar-after") || "0", 10) || 0;
    const limit = parseInt(request.headers.get("x-radar-limit") || "0", 10) || 0;
    const headers = new Headers(request.headers);
    for (const h of DROP) headers.delete(h);
    const init = { method: request.method, headers, redirect: "follow" };
    if (!["GET", "HEAD"].includes(request.method)) init.body = await request.arrayBuffer();

    let resp;
    try {
      resp = await fetch(`https://${host}/${rest.join("/")}${url.search}`, init);
    } catch (e) {
      return new Response(`relay error: ${e}`, { status: 502 });
    }
    if ((host === "t.me" || host === "telegram.me") && rest[0] === "s" && resp.ok && request.method === "GET") {
      const html = await resp.text();
      try {
        return slimTelegram(html, after, limit);
      } catch (e) {
        return new Response(html, { status: resp.status, headers: { "content-type": "text/html; charset=utf-8",
                                                                      "x-radar-slim": `error: ${e}` } });
      }
    }
    if (host === "news.google.com" && rest[0] === "rss" && resp.ok && request.method === "GET") {
      const xml = await resp.text();
      try {
        return slimRss(xml);
      } catch (e) {
        return new Response(xml, { status: resp.status, headers: { "content-type": "application/rss+xml; charset=utf-8" } });
      }
    }
    return resp;
  },
};

const json = (obj, status = 200) => new Response(JSON.stringify(obj), {
  status, headers: { "content-type": "application/json; charset=utf-8", "x-radar-slim": `v${VERSION}` } });

const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36";

async function gnResolve(id) {
  // 1) страница статьи Google: иногда сразу редирект на сайт или готовый адрес в data-n-au
  let page = await fetch(`https://news.google.com/articles/${id}?hl=ru&gl=RU&ceid=RU:ru`,
                         { headers: { "user-agent": UA, "accept-language": "ru,en;q=0.8" }, redirect: "follow" });
  if (page.status === 429 || page.status === 503) return { status: page.status, error: "google limited" };
  if (page.url && !page.url.includes("news.google.com") && !page.url.includes("consent.google")) {
    return { url: page.url, how: "redirect" };
  }
  let html = await page.text();
  const direct = pick(/data-n-au="([^"]+)"/, html);
  if (direct) return { url: direct.replace(/&amp;/g, "&"), how: "page" };
  let sg = pick(/data-n-a-sg="([^"]+)"/, html), ts = pick(/data-n-a-ts="([^"]+)"/, html);
  if (!sg || !ts) {
    page = await fetch(`https://news.google.com/rss/articles/${id}`, { headers: { "user-agent": UA } });
    html = await page.text();
    sg = pick(/data-n-a-sg="([^"]+)"/, html);
    ts = pick(/data-n-a-ts="([^"]+)"/, html);
    if (!sg || !ts) return { status: page.status, error: "no decoding params" };
  }
  // 2) внутренний запрос Google News «дай адрес статьи» (тот же, что делает их сайт)
  const inner = `["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],` +
                `"X","X",1,[1,1,1],1,1,null,0,0,null,0],"${id}",${ts},"${sg}"]`;
  const r = await fetch("https://news.google.com/_/DotsSplashUi/data/batchexecute", {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded;charset=UTF-8", "user-agent": UA },
    body: "f.req=" + encodeURIComponent(JSON.stringify([[["Fbv4je", inner]]])),
  });
  if (r.status === 429 || r.status === 503) return { status: r.status, error: "google limited" };
  const txt = await r.text();
  // ответ: «)]}'», пустая строка, строка-массив [["wrb.fr","Fbv4je","[\"garturlres\",\"https://…\",1]",…]], служебное
  const line = txt.split("\n").find(l => l.startsWith('[["wrb.fr"'));
  let url = "";
  try {
    url = JSON.parse(JSON.parse(line)[0][2])[1];
  } catch (e) {
    url = (pick(/garturlres\\",\\"(https?:[^"\\]+)/, txt) || "").replace(/\\u003d/g, "=").replace(/\\u0026/g, "&");
  }
  return url ? { url, how: "batchexecute" } : { status: r.status, error: "no url" };
}

function slimRss(xml) {
  const parts = xml.split("<item>");
  const items = parts.slice(1).map(s => s.split("</item>")[0]).slice(0, 25)
    .map(it => "<item>" + it.replace(/<description>[\s\S]*?<\/description>/, "") + "</item>");
  const body = parts[0] + items.join("\n") + "</channel></rss>";
  return new Response(body, { headers: { "content-type": "application/rss+xml; charset=utf-8",
                                         "x-radar-slim": `v${VERSION} rss ${items.length}/${parts.length - 1}` } });
}

const pick = (re, s) => { const m = re.exec(s); return m ? m[1] : ""; };

function cut(s, n) {           // обрезка без «рваного» тега или сущности на конце
  if (s.length <= n) return s;
  return s.slice(0, n).replace(/<[^>]*$/, "").replace(/&[#a-zA-Z0-9]*$/, "") + "…";
}

function slimTelegram(html, after, limit) {
  const blocks = html.split('<div class="tgme_widget_message_wrap').slice(1);
  if (!blocks.length) {        // разметка поменялась или канал пуст — отдаём как есть
    return new Response(html, { headers: { "content-type": "text/html; charset=utf-8", "x-radar-slim": "raw" } });
  }
  const title = pick(/tgme_channel_info_header_title[^>]*>\s*(?:<span[^>]*>)?([^<]*)/, html);
  const counters = [...html.matchAll(/<span class="counter_value">([^<]+)<\/span>\s*<span class="counter_type">([^<]+)<\/span>/g)]
    .map(m => `<div class="tgme_channel_info_counter"><span class="counter_value">${m[1]}</span> <span class="counter_type">${m[2]}</span></div>`)
    .join("");
  let posts = [];
  for (const b of blocks) {
    const post = pick(/data-post="([^"]+)"/, b);
    const id = parseInt(post.split("/").pop(), 10);
    if (!post || !id || id <= after) continue;
    // свой текст поста — div с js-message_text; цитата поста, на который ответили, — js-message_reply_text
    const clean = s => s
      .replace(/<i class="emoji"[^>]*>(?:<b>)?([\s\S]*?)(?:<\/b>)?<\/i>/g, "$1")
      .replace(/<(tg-emoji|span|a)\b[^>]*>/g, "").replace(/<\/(tg-emoji|span|a)>/g, "");
    const replyText = cut(clean(pick(/<div class="[^"]*js-message_reply_text[^"]*"[^>]*>([\s\S]*?)<\/div>/, b)), 400);
    const replyTo = pick(/<a class="tgme_widget_message_reply[^"]*"[^>]*href="https?:\/\/t\.me\/(?:s\/)?([^"?#]+)/, b);
    let text = pick(/<div class="tgme_widget_message_text[^"]*js-message_text[^"]*"[^>]*>([\s\S]*?)<\/div>/, b) ||
      pick(/<div class="tgme_widget_message_text(?![^"]*js-message_reply_text)[^"]*"[^>]*>([\s\S]*?)<\/div>/, b);
    text = cut(clean(text), 3000);
    const pTitle = pick(/link_preview_title[^>]*>([\s\S]*?)<\/div>/, b);
    const pDesc = cut(pick(/link_preview_description[^>]*>([\s\S]*?)<\/div>/, b), 600);
    const time = pick(/<time[^>]*datetime="([^"]+)"/, b);
    const views = pick(/tgme_widget_message_views">([^<]*)</, b);
    const fwd = pick(/tgme_widget_message_forwarded_from_name"[^>]*>(?:<span[^>]*>)?([^<]*)/, b);
    const photos = [...b.matchAll(/tgme_widget_message_photo_wrap[^>]*background-image:url\('([^']+)'\)/g)].slice(0, 3);
    posts.push({ id, html:
      `<div class="tgme_widget_message" data-post="${post}">` +
      (fwd ? `<a class="tgme_widget_message_forwarded_from_name">${fwd}</a>` : "") +
      (replyText || replyTo ? `<a class="tgme_widget_message_reply" href="https://t.me/${replyTo}">` +
        `<div class="tgme_widget_message_text js-message_reply_text">${replyText}</div></a>` : "") +
      photos.map(m => `<a class="tgme_widget_message_photo_wrap" style="background-image:url('${m[1]}')"></a>`).join("") +
      `<div class="tgme_widget_message_text js-message_text">${text}</div>` +
      (pTitle || pDesc ? `<div class="tgme_widget_message_link_preview">${pTitle}<br/>${pDesc}</div>` : "") +
      `<span class="tgme_widget_message_views">${views}</span><time datetime="${time}"></time></div>` });
  }
  posts.sort((a, b) => b.id - a.id);
  if (limit) posts = posts.slice(0, limit);
  const body = `<html><body><div class="tgme_channel_info_header_title"><span>${title}</span></div>${counters}\n` +
               posts.map(p => p.html).join("\n") + "</body></html>";
  return new Response(body, { headers: { "content-type": "text/html; charset=utf-8",
                                         "x-radar-slim": `v${VERSION} ${posts.length}/${blocks.length}` } });
}
