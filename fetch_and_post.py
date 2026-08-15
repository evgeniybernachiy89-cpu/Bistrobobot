"""
AlphaFeed news bot v2
----------------------
Тянет новости из расширенного списка RSS + Polymarket, прогоняет весь
пакет через один batch-запрос к Gemini (бесплатный тир) для:
  - фильтра по теме (финансы/крипта/Polymarket, без Украины)
  - смыслового дедупа (не по ссылке, а по сути события)
  - определения "это апдейт уже опубликованной новости" -> ответ в Telegram
  - ранжирования важности -> топовые с ⚡️ идут первыми
  - пометки организаций/лиц из вашего локального списка иноагентов

ВАЖНО про иноагентов: я не встраиваю сюда никакие конкретные имена или
организации. Официальный реестр Минюста живой и меняется каждую неделю
(minjust.gov.ru) — заполняйте и обновляйте FLAGGED_ENTITIES сами по
актуальному реестру. Gemini дополнительно попробует заметить очевидные
случаи, но это не юридически надёжный источник — авто-эвристика Gemini
только логируется как "проверьте вручную", в пост не подставляется.

Без GEMINI_API_KEY эта версия скрипта не даёт большую часть обещанной
логики (дедуп/ранжирование/фильтр темы) — ключ обязателен.
"""

import os
import re
import json
import time
import hashlib
import datetime
import requests
import feedparser

# ---------- Настройки ----------

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

STATE_FILE = "posted.json"
HISTORY_KEEP_HOURS = 48   # сколько часов храним историю постов для сравнения на дубли/апдейты
HISTORY_MAX_ITEMS = 120

# Расширенный список источников — англоязычные крипто/финансовые СМИ +
# несколько русскоязычных. RSS "всего мирового поля" физически не бывает
# бесплатным без платных агрегаторов (типа NewsAPI/GDELT с ключом) — это
# максимально широкий бесплатный набор публичных RSS без ключей.
FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
    "https://cryptoslate.com/feed/",
    "https://news.bitcoin.com/feed/",
    "https://www.theblock.co/rss.xml",
    "https://www.newsbtc.com/feed/",
    "https://bitcoinist.com/feed/",
    "https://u.today/rss",
    "https://cryptopotato.com/feed/",
    "https://ambcrypto.com/feed/",
    "https://watcher.guru/news/feed",
    "https://forklog.com/feed",              # русскоязычный крипто-источник
    "https://ru.investing.com/rss/news.rss", # финансы/макро на русском
]

# Polymarket — публичное REST API, ключ не нужен
POLYMARKET_MARKETS_URL = "https://gamma-api.polymarket.com/markets"

# ⚡️ ставится на топовые новости при отправке
TOP_EMOJI = "⚡️"

# ---------- Локальный список иноагентов/запрещённых организаций ----------
# ЗАПОЛНЯЙТЕ И ОБНОВЛЯЙТЕ САМИ по официальному реестру Минюста:
# https://minjust.gov.ru/ru/documents/7755/
# Формат: "название или ФИО, как оно может встретиться в тексте": "метка"
FLAGGED_ENTITIES = {
    # "Иван Иванов": "признан(а) иностранным агентом в РФ",
    # "Meta Platforms": "деятельность признана экстремистской и запрещена в РФ",
}


def load_state():
    """Загружает состояние. Устойчиво к старому формату файла (без history)
    и к битому/пустому файлу — в этих случаях недостающие ключи создаются."""
    state = {"posted_ids": [], "history": []}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                state["posted_ids"] = loaded.get("posted_ids", []) or []
                state["history"] = loaded.get("history", []) or []
        except Exception as e:
            print(f"Не смог прочитать {STATE_FILE}, начинаю с чистого состояния: {e}")
    return state


def save_state(state):
    state["posted_ids"] = (state.get("posted_ids") or [])[-1000:]
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=HISTORY_KEEP_HOURS)
    history = [h for h in (state.get("history") or []) if h.get("ts", "") >= cutoff.isoformat()]
    state["history"] = history[-HISTORY_MAX_ITEMS:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def entry_id(link_or_title):
    return hashlib.sha256(link_or_title.encode("utf-8")).hexdigest()[:16]


def clean_html(html):
    return re.sub("<[^<]+?>", "", html or "").strip()


def get_image_url(entry):
    media_content = entry.get("media_content")
    if media_content:
        for m in media_content:
            if m.get("url"):
                return m["url"]
    media_thumb = entry.get("media_thumbnail")
    if media_thumb:
        for m in media_thumb:
            if m.get("url"):
                return m["url"]
    for link in entry.get("links", []):
        if link.get("type", "").startswith("image") and link.get("href"):
            return link["href"]
    html = entry.get("summary", "") or entry.get("description", "")
    match = re.search(r'<img[^>]+src="([^"]+)"', html)
    if match:
        return match.group(1)
    return None


def translate_to_ru(text):
    if not text.strip():
        return text
    url = "https://translate.googleapis.com/translate_a/single"
    params = {"client": "gtx", "sl": "auto", "tl": "ru", "dt": "t", "q": text}
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        return "".join(chunk[0] for chunk in data[0] if chunk[0]).strip() or text
    except Exception as e:
        print(f"Перевод не удался: {e}")
        return text


# ---------- Сбор кандидатов ----------

def fetch_rss_candidates():
    candidates = []
    for feed_url in FEEDS:
        try:
            parsed = feedparser.parse(feed_url)
        except Exception as e:
            print(f"Ошибка ленты {feed_url}: {e}")
            continue
        source_name = parsed.feed.get("title", feed_url)
        for entry in parsed.entries:
            link = entry.get("link", "")
            title = entry.get("title", "").strip()
            if not link or not title:
                continue
            candidates.append({
                "cid": entry_id(link),
                "title": title,
                "summary": clean_html(entry.get("summary", "") or entry.get("description", "")),
                "link": link,
                "source": source_name,
                "image_url": get_image_url(entry),
            })
    return candidates


def fetch_polymarket_candidates():
    """Забирает несколько заметных (по объёму) активных рынков Polymarket
    как потенциальные кандидаты для поста. Best-effort: если API недоступно
    или формат ответа изменится — просто возвращаем пустой список."""
    try:
        params = {"active": "true", "closed": "false", "order": "volume", "ascending": "false", "limit": 15}
        r = requests.get(POLYMARKET_MARKETS_URL, params=params, timeout=15)
        r.raise_for_status()
        markets = r.json()
    except Exception as e:
        print(f"Polymarket недоступен: {e}")
        return []

    candidates = []
    for m in markets:
        question = m.get("question") or m.get("title")
        if not question:
            continue
        slug = m.get("slug", "")
        link = f"https://polymarket.com/event/{slug}" if slug else "https://polymarket.com"
        candidates.append({
            "cid": entry_id(link + question),
            "title": f"Polymarket: {question}",
            "summary": m.get("description", "") or "",
            "link": link,
            "source": "Polymarket",
            "image_url": m.get("image") or None,
        })
    return candidates


# ---------- Gemini: один batch-запрос на весь пакет ----------

def classify_batch(candidates, history):
    """Отправляет весь пакет кандидатов + недавнюю историю постов в Gemini
    одним запросом. Возвращает dict cid -> classification, либо None при сбое
    (тогда main() работает в упрощённом резервном режиме)."""
    if not GEMINI_API_KEY or not candidates:
        return None

    history_brief = [
        {"message_id": h["message_id"], "title": h["title"], "summary": h.get("summary", "")[:200]}
        for h in history
    ]
    cand_brief = [
        {"cid": c["cid"], "title": c["title"], "summary": c["summary"][:400], "source": c["source"]}
        for c in candidates
    ]

    prompt = f"""Ты — редактор Telegram-канала о финансах и криптовалюте Alpha Feed.

Тебе дан список НОВЫХ кандидатов на публикацию (candidates) и список УЖЕ
ОПУБЛИКОВАННЫХ на канале за последние {HISTORY_KEEP_HOURS} часов постов (history).

Для КАЖДОГО кандидата верни объект со следующими полями:
- cid: тот же cid, что во входных данных
- relevant: true/false — тема канала это ТОЛЬКО финансы, криптовалюта,
  макроэкономика, регулирование, и интересные ставки Polymarket. Всё
  остальное (не по теме) — false.
- mentions_ukraine: true/false — упоминается ли Украина, война, боевые
  действия, любые события, связанные с Украиной. Если true — кандидат не
  публикуется вообще, независимо от relevant.
- duplicate_of_message_id: если этот кандидат описывает ТОТ ЖЕ инфоповод,
  что уже есть в history (пусть даже другими словами, из другого
  источника) — верни message_id того поста из history. Иначе null.
- update_of_message_id: если это НЕ дубль, а развитие/уточнение/апдейт
  уже опубликованной истории из history (новые цифры, новый поворот той
  же истории) — верни message_id той истории. Иначе null.
- importance: "top" если новость реально значимая для рынка (крупные суммы,
  регуляторные решения, обвалы/взлёты, крупные хаки, заявления ФРС и т.п.),
  иначе "normal".
- flagged_entities: список имён/названий организаций из текста, которые
  МОГУТ быть иностранными агентами или запрещёнными в РФ организациями
  (просто твоя лучшая догадка по общеизвестным случаям, не авторитетный
  источник) — если сомневаешься, не включай.

Верни СТРОГО валидный JSON-массив объектов, без markdown-разметки,
без пояснений до или после, ничего кроме JSON.

candidates = {json.dumps(cand_brief, ensure_ascii=False)}

history = {json.dumps(history_brief, ensure_ascii=False)}
"""

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
    )
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    try:
        r = requests.post(url, json=payload, timeout=60)
        r.raise_for_status()
        data = r.json()
        raw = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        raw = re.sub(r"^```json|```$", "", raw.strip(), flags=re.MULTILINE).strip()
        parsed = json.loads(raw)
        return {item["cid"]: item for item in parsed if "cid" in item}
    except Exception as e:
        print(f"Gemini batch-классификация не удалась: {e}")
        return None


# ---------- Форматирование и отправка ----------

def apply_entity_labels(text):
    for name, label in FLAGGED_ENTITIES.items():
        if name.lower() in text.lower():
            text += f"\n\n⚠️ {name} — {label}"
    return text


def build_post_text(candidate, is_top):
    title_ru = translate_to_ru(candidate["title"])
    summary_ru = translate_to_ru(candidate["summary"][:500])
    prefix = f"{TOP_EMOJI} " if is_top else ""
    text = f"*{prefix}{title_ru}*\n\n{summary_ru}"
    text = apply_entity_labels(text)
    return text.strip()


def send_to_telegram(text, image_url=None, reply_to=None):
    base = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    if image_url:
        caption = text if len(text) <= 1024 else text[:1000].rsplit(" ", 1)[0] + "…"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "photo": image_url,
            "caption": caption,
            "parse_mode": "Markdown",
        }
        if reply_to:
            payload["reply_to_message_id"] = reply_to
        r = requests.post(f"{base}/sendPhoto", json=payload, timeout=20)
        if r.ok:
            return r.json()["result"]["message_id"]
        print(f"sendPhoto не сработал ({r.status_code}: {r.text}), пробую без фото")

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    r = requests.post(f"{base}/sendMessage", json=payload, timeout=20)
    if not r.ok:
        print(f"Telegram error {r.status_code}: {r.text}")
        return None
    return r.json()["result"]["message_id"]


# ---------- main ----------

def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise SystemExit("Не заданы TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.")

    state = load_state()
    posted_ids = set(state["posted_ids"])
    history = state["history"]

    raw_candidates = fetch_rss_candidates() + fetch_polymarket_candidates()
    # первый, дешёвый слой дедупа — по точной ссылке/id, без всякого ИИ
    candidates = [c for c in raw_candidates if c["cid"] not in posted_ids]

    if not candidates:
        print("Новых кандидатов нет.")
        return

    classifications = classify_batch(candidates, history)

    scored = []
    for c in candidates:
        cls = (classifications or {}).get(c["cid"])

        if cls is None:
            # Резервный режим без Gemini/при сбое: публикуем как обычную
            # новость без ранжирования и без смыслового дедупа — только
            # базовая проверка по ссылке (уже сделана выше).
            scored.append((c, {"importance": "normal", "update_of_message_id": None}))
            continue

        if not cls.get("relevant", True):
            continue
        if cls.get("mentions_ukraine"):
            continue
        if cls.get("duplicate_of_message_id"):
            continue

        for name in cls.get("flagged_entities", []) or []:
            if name not in FLAGGED_ENTITIES:
                print(f"[проверьте вручную] Gemini предполагает иноагента/запрет: {name}")

        scored.append((c, cls))

    # топовые (⚡️) — первыми
    scored.sort(key=lambda pair: 0 if pair[1].get("importance") == "top" else 1)

    sent = 0
    for c, cls in scored:
        is_top = cls.get("importance") == "top"
        text = build_post_text(c, is_top)

        reply_to = None
        update_id = cls.get("update_of_message_id")
        if update_id:
            reply_to = update_id
            text = "🔄 Уточнение по ранее опубликованной новости:\n\n" + text

        hashtags = "#AlphaFeedru"
        text = f"{text}\n\n{hashtags}"

        message_id = send_to_telegram(text, c.get("image_url"), reply_to)
        if message_id:
            posted_ids.add(c["cid"])
            history.append({
                "cid": c["cid"],
                "message_id": message_id,
                "title": c["title"],
                "summary": c["summary"][:300],
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            })
            sent += 1
            time.sleep(2)

    state["posted_ids"] = list(posted_ids)
    state["history"] = history
    save_state(state)
    print(f"Готово. Отправлено новых постов: {sent}")


if __name__ == "__main__":
    main()