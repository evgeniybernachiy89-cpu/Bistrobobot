"""
AlphaFeed news bot
-------------------
Тянет свежие новости из RSS-лент криптоСМИ, отсеивает старьё и дубли,
форматирует пост в стиле Alpha Feed и отправляет в Telegram-канал.

Все секреты (токен бота, chat_id, ключ LLM) читаются из переменных
окружения — их нужно задать в GitHub Actions Secrets, а НЕ вписывать
в этот файл.
"""

import os
import json
import time
import hashlib
import datetime
import requests
import feedparser

# ---------- Настройки ----------

# Сколько последних часов считаем "свежим" (можно переопределить в workflow)
HOURS_WINDOW = float(os.environ.get("HOURS_WINDOW", "3"))

# RSS-ленты. Можно добавлять свои — просто добавь URL в список.
FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
    "https://cryptoslate.com/feed/",
    "https://news.bitcoin.com/feed/",
    "https://www.theblock.co/rss.xml",
]

STATE_FILE = "posted.json"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# LLM опционален. Если хотите живой стиль Alpha Feed, а не просто
# заголовок+ссылку — задайте GEMINI_API_KEY в секретах (бесплатный тир
# Google Gemini). Если ключа нет — бот просто шлёт аккуратный шаблон
# без LLM, это тоже нормально работает.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# Ключевые слова -> хэштеги (простая эвристика без LLM)
HASHTAG_MAP = {
    "bitcoin": "#Bitcoin", "btc": "#Bitcoin",
    "ethereum": "#Ethereum", "eth ": "#Ethereum",
    "binance": "#Binance",
    "etf": "#ETF",
    "fed": "#ФРС", "federal reserve": "#ФРС", "interest rate": "#ставки",
    "sec": "#SEC", "regulat": "#регулирование",
    "hack": "#взлом", "exploit": "#взлом",
    "defi": "#DeFi",
    "stablecoin": "#стейблкоины",
    "solana": "#Solana", "sol ": "#Solana",
    "trump": "#Trump",
    "gold": "#золото", "oil": "#нефть",
    "nasdaq": "#Nasdaq", "s&p": "#SP500",
    "ai ": "#AI", "artificial intelligence": "#AI",
}


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"posted_ids": []}


def save_state(state):
    # держим только последние 500 id, чтобы файл не рос бесконечно
    state["posted_ids"] = state["posted_ids"][-500:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def entry_id(entry):
    raw = entry.get("id") or entry.get("link") or entry.get("title", "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def entry_age_hours(entry):
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            published = datetime.datetime(*t[:6], tzinfo=datetime.timezone.utc)
            now = datetime.datetime.now(datetime.timezone.utc)
            return (now - published).total_seconds() / 3600
    return None  # неизвестно — пропустим на всякий случай


def translate_to_ru(text):
    """Бесплатный перевод через неофициальный endpoint Google Translate.
    Ключ и регистрация не нужны. Если сервис недоступен — возвращаем
    оригинальный текст, чтобы бот не падал."""
    if not text.strip():
        return text
    url = "https://translate.googleapis.com/translate_a/single"
    params = {
        "client": "gtx",
        "sl": "auto",
        "tl": "ru",
        "dt": "t",
        "q": text,
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        # data[0] — список кусков [[перевод, оригинал, ...], ...]
        translated = "".join(chunk[0] for chunk in data[0] if chunk[0])
        return translated.strip() or text
    except Exception as e:
        print(f"Перевод не удался, оставляю оригинал: {e}")
        return text


def get_image_url(entry):
    """Пытается найти картинку в RSS-записи разными способами.
    Возвращает URL картинки или None, если не нашли."""
    # 1. media:content (частый вариант у CoinDesk, CoinTelegraph и т.п.)
    media_content = entry.get("media_content")
    if media_content:
        for m in media_content:
            if m.get("url"):
                return m["url"]

    # 2. media:thumbnail
    media_thumb = entry.get("media_thumbnail")
    if media_thumb:
        for m in media_thumb:
            if m.get("url"):
                return m["url"]

    # 3. enclosure (обычный RSS-способ приложить картинку)
    for link in entry.get("links", []):
        if link.get("type", "").startswith("image") and link.get("href"):
            return link["href"]

    # 4. картинка внутри HTML описания (<img src="...">)
    import re
    html = entry.get("summary", "") or entry.get("description", "")
    match = re.search(r'<img[^>]+src="([^"]+)"', html)
    if match:
        return match.group(1)

    return None


def guess_hashtags(text):
    text_low = text.lower()
    tags = []
    for kw, tag in HASHTAG_MAP.items():
        if kw in text_low and tag not in tags:
            tags.append(tag)
        if len(tags) >= 4:
            break
    if not tags:
        tags = ["#крипта"]
    return tags


def format_template(entry, source_name):
    title_en = entry.get("title", "").strip()
    summary_en = entry.get("summary", "") or entry.get("description", "")
    # грубая чистка html-тегов
    import re
    summary_en = re.sub("<[^<]+?>", "", summary_en).strip()
    if len(summary_en) > 500:
        summary_en = summary_en[:500].rsplit(" ", 1)[0] + "…"

    # хэштеги ищем по оригинальному (английскому) тексту — словарь ключевых
    # слов у нас на английском, так надёжнее
    hashtags = " ".join(guess_hashtags(title_en + " " + summary_en) + ["#AlphaFeedru"])

    # переводим на русский для самого поста
    title_ru = translate_to_ru(title_en)
    summary_ru = translate_to_ru(summary_en)

    text = (
        f"*{title_ru}*\n\n"
        f"{summary_ru}\n\n"
        f"{hashtags}"
    )
    return text


def format_with_gemini(entry, source_name):
    """Опционально переписывает пост в стиле Alpha Feed через Gemini (бесплатный тир)."""
    import re
    title = entry.get("title", "").strip()
    summary = entry.get("summary", "") or entry.get("description", "")
    summary = re.sub("<[^<]+?>", "", summary).strip()
    link = entry.get("link", "")

    prompt = f"""Ты редактор Telegram-канала Alpha Feed (крипто/финансы).
Стиль: коротко, дерзко, с характером, без канцелярщины и ИИ-штампов.
На основе новости ниже напиши пост на русском: цепляющий заголовок без кликбейта,
3-5 предложений сути, затем 3-5 хэштегов включая #AlphaFeedru.

Заголовок источника: {title}
Текст источника: {summary}
"""

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
    )
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    try:
        r = requests.post(url, json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()
        generated = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        return generated
    except Exception as e:
        print(f"Gemini formatting failed, falling back to template: {e}")
        return format_template(entry, source_name)


def send_to_telegram(text, image_url=None):
    if image_url:
        # у Telegram лимит подписи к фото — 1024 символа, обрежем при необходимости
        caption = text if len(text) <= 1024 else text[:1000].rsplit(" ", 1)[0] + "…"
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "photo": image_url,
            "caption": caption,
            "parse_mode": "Markdown",
        }
        r = requests.post(url, json=payload, timeout=20)
        if r.ok:
            return True
        print(f"sendPhoto не сработал ({r.status_code}: {r.text}), пробую без картинки")
        # если Telegram не смог скачать картинку по ссылке — падаем обратно на текст

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    r = requests.post(url, json=payload, timeout=20)
    if not r.ok:
        print(f"Telegram error {r.status_code}: {r.text}")
    return r.ok


def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise SystemExit(
            "Не заданы TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID. "
            "Добавьте их в GitHub Actions Secrets."
        )

    state = load_state()
    posted_ids = set(state["posted_ids"])
    sent_count = 0

    for feed_url in FEEDS:
        try:
            parsed = feedparser.parse(feed_url)
        except Exception as e:
            print(f"Feed error {feed_url}: {e}")
            continue

        source_name = parsed.feed.get("title", feed_url)

        for entry in parsed.entries:
            eid = entry_id(entry)
            if eid in posted_ids:
                continue

            age = entry_age_hours(entry)
            if age is None or age > HOURS_WINDOW:
                continue

            if GEMINI_API_KEY:
                text = format_with_gemini(entry, source_name)
            else:
                text = format_template(entry, source_name)

            image_url = get_image_url(entry)

            ok = send_to_telegram(text, image_url)
            if ok:
                posted_ids.add(eid)
                sent_count += 1
                time.sleep(2)  # не спамим Telegram API

    state["posted_ids"] = list(posted_ids)
    save_state(state)
    print(f"Готово. Отправлено новых постов: {sent_count}")


if __name__ == "__main__":
    main()