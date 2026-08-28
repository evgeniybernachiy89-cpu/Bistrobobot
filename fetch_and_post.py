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
import random
import hashlib
import datetime
import requests
import feedparser

# ---------- Настройки ----------

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# Тема запуска: "crypto" или "macro". Задаётся в workflow.
TOPIC = os.environ.get("TOPIC", "all").lower()

# У каждой темы своё состояние — иначе два параллельных запуска
# подрались бы при коммите одного и того же файла в репозиторий.
STATE_FILE = os.environ.get("STATE_FILE", "posted.json")

# Состояние до разделения на темы. Читаем только на чтение, чтобы
# новости, опубликованные ДО перехода на три задачи, не вышли повторно.
LEGACY_STATE_FILE = "posted.json"

# Не публикуем новости старше этого возраста. Ленты отдают записи за
# несколько дней, и без этого ограничения при пустом состоянии в канал
# хлынет всё подряд, включая недельную давность.
MAX_AGE_HOURS = float(os.environ.get("MAX_AGE_HOURS", "24"))
HISTORY_KEEP_HOURS = 96   # сколько часов храним историю постов для сравнения на дубли/апдейты
HISTORY_MAX_ITEMS = 500

# Сколько id обработанных новостей помним. При ~300 кандидатах в час
# прежнего лимита в 1000 хватало всего на три часа, после чего бот
# начинал заново перебирать уже виденное.
POSTED_IDS_LIMIT = 20000

# Сколько постов максимум за один запуск. При запуске раз в час это и есть
# "не больше N постов в час". Меняется в news.yml через MAX_POSTS_PER_RUN.
MAX_POSTS_PER_RUN = int(os.environ.get("MAX_POSTS_PER_RUN", "5"))

# Случайная задержка перед публикацией (минут), чтобы посты не выходили
# ровно по минутам cron. Запуск остаётся коротким.
JITTER_MAX_MINUTES = int(os.environ.get("JITTER_MAX_MINUTES", "7"))

# Целевой темп публикаций в час. Если планировщик GitHub пропустил
# запуски (на бесплатном тарифе это обычное дело), бот наверстает:
# опубликует столько постов, сколько "задолжал" за простой.
POSTS_PER_HOUR = int(os.environ.get("POSTS_PER_HOUR", "4"))

# Публиковать только новости, помеченные Gemini как важные (importance=top).
# Если поставить "false" — вернётся публикация всего подряд.
TOP_ONLY = os.environ.get("TOP_ONLY", "true").lower() == "true"

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

# Ленты для темы "tech" — подключаются только при TOPIC=tech,
# чтобы крипто- и макро-задачи не тянули лишнее.
TECH_FEEDS = [
    "https://www.theverge.com/rss/index.xml",
    "https://feeds.arstechnica.com/arstechnica/index",
    "https://www.tomshardware.com/feeds/all",
    "https://techcrunch.com/feed/",
    "https://9to5mac.com/feed/",
    "https://videocardz.com/feed",
    "https://www.engadget.com/rss.xml",
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


# Слова-маркеры значимости — используются ТОЛЬКО в резервном режиме,
# когда Gemini недоступен (нет ключа, сбой, исчерпан лимит).
FALLBACK_TOP_KEYWORDS = [
    "fed", "federal reserve", "sec ", "etf", "hack", "exploit", "stolen",
    "lawsuit", "ban", "bankrupt", "billion", "million", "surge", "plunge",
    "crash", "rally", "whale", "listing", "delist", "approval", "rate cut",
    "rate hike", "inflation", "treasury", "regulation", "seizure", "fine",
]

# Слова-маркеры периферии — в резервном режиме такие новости не публикуются.
FALLBACK_SKIP_KEYWORDS = [
    "price prediction", "technical analysis", "could hit", "here's why",
    "top 5", "top 10", "best ", "guide", "how to", "sponsored", "opinion",
    "what to expect", "forecast", "review",
]

# Слова, при которых новость не публикуется никогда (тема Украины).
HARD_SKIP_KEYWORDS = ["ukraine", "ukrainian", "kyiv", "kiev", "украин", "киев"]


def fallback_classify(candidate):
    """Грубая оценка без Gemini: по ключевым словам. Возвращает
    классификацию в том же формате либо None, если публиковать не стоит.
    Смысловой дедуп и определение апдейтов тут невозможны — только
    базовая фильтрация."""
    text = f"{candidate['title']} {candidate['summary']}".lower()

    for kw in HARD_SKIP_KEYWORDS:
        if kw in text:
            return None

    for kw in FALLBACK_SKIP_KEYWORDS:
        if kw in text:
            return None

    hits = sum(1 for kw in FALLBACK_TOP_KEYWORDS if kw in text)
    if hits == 0:
        return None

    return {
        "importance": "top" if hits >= 2 else "normal",
        "update_of_message_id": None,
        "fallback": True,
    }


# ---------- Локальный дедуп без ИИ ----------

# Служебные слова, которые не несут смысла при сравнении заголовков
STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "as", "is", "are", "was", "were", "be",
    "been", "has", "have", "had", "will", "would", "can", "could", "may",
    "says", "say", "said", "after", "before", "over", "under", "into",
    "its", "it", "this", "that", "these", "those", "new", "now", "amid",
    "not", "more", "than", "then", "how", "why", "what", "who", "you",
    "и", "в", "на", "с", "по", "за", "из", "от", "до", "для", "что",
    "как", "это", "все", "уже", "его", "их", "не", "но", "или", "к",
}


# Синонимы: разные издания называют одно и то же по-разному
SYNONYMS = {
    "federal": "fed", "reserve": "fed", "fed": "fed",
    "microstrategy": "strategy", "strategy": "strategy",
    "btc": "bitcoin", "bitcoin": "bitcoin",
    "eth": "ethereum", "ether": "ethereum", "ethereum": "ethereum",
    "etfs": "etf", "etf": "etf",
    "hack": "exploit", "hacked": "exploit", "hackers": "exploit",
    "hacker": "exploit", "exploit": "exploit", "exploited": "exploit",
    "stolen": "exploit", "steal": "exploit", "steals": "exploit",
    "drained": "exploit", "drain": "exploit", "breach": "exploit",
    "rates": "rate", "rate": "rate", "interest": "rate",
    "buys": "buy", "bought": "buy", "adds": "buy", "acquires": "buy",
    "purchase": "buy", "purchases": "buy", "buy": "buy",
    "inflows": "inflow", "inflow": "inflow",
    "outflows": "outflow", "outflow": "outflow",
    "tokens": "token", "token": "token",
    "prices": "price", "price": "price",
    "lists": "listing", "listed": "listing", "listing": "listing",
}


def _stem(word):
    """Грубая нормализация окончаний: etfs -> etf, holds -> hold."""
    if word in SYNONYMS:
        return SYNONYMS[word]
    for suffix in ("ing", "ed", "es", "s"):
        if len(word) > 4 and word.endswith(suffix):
            base = word[: -len(suffix)]
            return SYNONYMS.get(base, base)
    return word


def significant_tokens(text):
    """Выделяет значимые слова заголовка: имена, тикеры, суммы, действия.
    По их пересечению определяем, об одном ли событии речь."""
    text = (text or "").lower()
    # суммы: $40M и "$40 million" должны давать один и тот же токен.
    # ВАЖЕН порядок альтернатив — длинные варианты первыми, иначе "m"
    # съест начало слова "million" и оставит мусор "illion".
    text = re.sub(
        r"[$€£]\s?([\d][\d.,]*)\s?(trillion|billion|million|mln|bn|tn|[mbkt])?\b",
        lambda m: f" money{m.group(1).rstrip('.,').replace(',', '')} ",
        text,
    )
    # крупные числа без валюты: 5,000 BTC -> 5000
    text = re.sub(r"(\d),(\d{3})", r"\1\2", text)
    words = re.findall(r"[a-zа-яё0-9]+", text)
    return {_stem(w) for w in words if len(w) > 2 and w not in STOPWORDS}


def similarity(tokens_a, tokens_b):
    """Коэффициент Жаккара: доля общих слов от общего их числа."""
    if not tokens_a or not tokens_b:
        return 0.0
    union = len(tokens_a | tokens_b)
    return len(tokens_a & tokens_b) / union if union else 0.0


def is_same_story(tokens_a, tokens_b, threshold=0.40):
    """Событие считается тем же, если заголовки сильно пересекаются по
    значимым словам ЛИБО один почти целиком входит в другой (краткая и
    развёрнутая версии одной новости)."""
    common = len(tokens_a & tokens_b)
    # Меньше трёх общих слов — это совпадение темы, а не события.
    # Без этого условия "Fed cuts rates" и "Fed raises rates" слипаются.
    if common < 3:
        return False
    if similarity(tokens_a, tokens_b) >= threshold:
        return True
    smaller = min(len(tokens_a), len(tokens_b))
    if smaller >= 5 and common / smaller >= 0.8:
        return True
    return False


# ---------- Тематическое разделение ----------

CRYPTO_WORDS = [
    "bitcoin", "btc", "ethereum", "eth", "crypto", "blockchain", "defi",
    "altcoin", "solana", "xrp", "ripple", "binance", "coinbase", "kraken",
    "stablecoin", "usdt", "usdc", "tether", "nft", "web3", "token",
    "mining", "miner", "wallet", "exchange", "airdrop", "staking",
    "memecoin", "dogecoin", "shiba", "layer 2", "l2", "rollup", "dao",
    "bridge", "protocol", "onchain", "on-chain", "smart contract", "sandbox",
    "polygon", "avalanche", "cardano", "chainlink", "uniswap", "aave",
    "custody", "cold wallet", "hot wallet", "seed phrase", "validator",
    "биткоин", "эфириум", "крипт", "блокчейн", "токен", "майнинг",
]

MACRO_WORDS = [
    "fed", "federal reserve", "central bank", "ecb", "boj", "interest rate",
    "inflation", "cpi", "gdp", "unemployment", "jobs report", "treasury",
    "bond", "yield", "gold", "silver", "oil", "brent", "wti", "opec",
    "s&p", "nasdaq", "dow jones", "stock", "equities", "dollar", "euro",
    "recession", "tariff", "trade war", "commodity", "futures",
    "ставк", "инфляц", "нефть", "золото", "ввп", "облигац", "цб",
]


TECH_WORDS = [
    "iphone", "apple", "samsung", "google pixel", "android", "ios",
    "nvidia", "amd", "intel", "gpu", "cpu", "chip", "semiconductor",
    "rtx", "radeon", "ryzen", "geforce", "processor", "motherboard",
    "laptop", "smartphone", "tablet", "macbook", "ipad", "watch",
    "openai", "anthropic", "gemini", "chatgpt", "llm", "data center",
    "tsmc", "foundry", "nanometer", "benchmark", "overclock", "cooling",
    "ssd", "ram", "ddr5", "pcie", "firmware", "display", "camera",
    "айфон", "процессор", "видеокарт", "чип", "смартфон", "ноутбук",
]


def detect_topic(candidate):
    """Определяет, к какой теме относится новость: crypto или macro.
    Если попадает в обе — считаем криптой (профильная тема канала)."""
    text = f"{candidate['title']} {candidate['summary']}".lower()
    scores = {
        "crypto": sum(1 for w in CRYPTO_WORDS if w in text),
        "macro": sum(1 for w in MACRO_WORDS if w in text),
        "tech": sum(1 for w in TECH_WORDS if w in text),
    }
    best = max(scores, key=lambda k: scores[k])
    if scores[best] == 0:
        return None            # не наша тема вообще
    # Крипта — профильная тема канала: при равенстве очков она выигрывает.
    if scores["crypto"] == scores[best]:
        return "crypto"
    return best


def load_state():
    """Загружает состояние. Устойчиво к старому формату файла (без history)
    и к битому/пустому файлу — в этих случаях недостающие ключи создаются."""
    state = {"posted_ids": [], "history": [], "last_post_ts": None}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                state["posted_ids"] = loaded.get("posted_ids", []) or []
                state["history"] = loaded.get("history", []) or []
                state["last_post_ts"] = loaded.get("last_post_ts")
        except Exception as e:
            print(f"Не смог прочитать {STATE_FILE}, начинаю с чистого состояния: {e}")

    # Читаем состояния ДРУГИХ тем и старое общее — только на чтение.
    # Без этого новость, попавшая не в свою тему, вышла бы дважды:
    # задачи не видят публикаций друг друга.
    others = [LEGACY_STATE_FILE, "posted_crypto.json",
              "posted_macro.json", "posted_tech.json"]
    known = set(state["posted_ids"])
    borrowed = []
    for other in others:
        if other == STATE_FILE or not os.path.exists(other):
            continue
        try:
            with open(other, "r", encoding="utf-8") as f:
                data = json.load(f)
            for i in (data.get("posted_ids", []) or []):
                if i not in known:
                    known.add(i)
                    borrowed.append(i)
        except Exception as e:
            print(f"Не смог прочитать {other}: {e}")
    if borrowed:
        state["posted_ids"] = borrowed + state["posted_ids"]
        print(f"Учёл {len(borrowed)} публикаций из других тем и общей истории")
    return state


def save_state(state):
    # ВАЖНО: сохраняем порядок добавления. Раньше здесь оказывалось
    # множество (set), из-за чего обрезка [-N:] выбрасывала случайные
    # записи, а не старые — и бот начинал заново обрабатывать уже
    # виденные новости.
    ids = state.get("posted_ids") or []
    seen, ordered = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            ordered.append(i)
    state["posted_ids"] = ordered[-POSTED_IDS_LIMIT:]

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


def _parse_translate_response(data):
    """Разные endpoint'ы Google отдают разную структуру — разбираем обе."""
    # формат translate_a/single: [[["перевод","оригинал",...], ...], ...]
    if isinstance(data, list) and data and isinstance(data[0], list):
        parts = []
        for chunk in data[0]:
            if isinstance(chunk, list) and chunk and isinstance(chunk[0], str):
                parts.append(chunk[0])
            elif isinstance(chunk, str):
                parts.append(chunk)
        if parts:
            return "".join(parts).strip()
    # формат clients5 translate_a/t: ["перевод"] или "перевод"
    if isinstance(data, list) and data and isinstance(data[0], str):
        return data[0].strip()
    if isinstance(data, str):
        return data.strip()
    return ""


def _is_mostly_russian(text):
    """Если текст уже на русском — переводить не нужно (Forklog, Investing)."""
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return False
    cyrillic = sum(1 for ch in letters if "а" <= ch.lower() <= "я" or ch.lower() == "ё")
    return cyrillic / len(letters) > 0.5


def _try_translate_once(text, endpoint):
    """Одна попытка перевода через конкретный endpoint."""
    params = {"client": "gtx", "sl": "auto", "tl": "ru", "dt": "t", "q": text}
    headers = {
        # без User-Agent Google чаще отвечает 403 на запросы из облака
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0 Safari/537.36"
    }
    r = requests.get(endpoint, params=params, headers=headers, timeout=15)
    r.raise_for_status()
    return _parse_translate_response(r.json())


def _translate_mymemory(text):
    """Запасной переводчик — независимая от Google инфраструктура.
    Бесплатно ~5000 слов в сутки без ключа. Ограничение длины запроса —
    500 символов, поэтому длинный текст режем на куски по предложениям."""
    def chunks(s, limit=480):
        out, cur = [], ""
        for sentence in re.split(r"(?<=[.!?])\s+", s):
            if len(cur) + len(sentence) + 1 <= limit:
                cur = f"{cur} {sentence}".strip()
            else:
                if cur:
                    out.append(cur)
                cur = sentence[:limit]
        if cur:
            out.append(cur)
        return out

    translated = []
    for part in chunks(text):
        r = requests.get(
            "https://api.mymemory.translated.net/get",
            params={"q": part, "langpair": "en|ru"},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        piece = (data.get("responseData") or {}).get("translatedText", "")
        if not piece:
            raise RuntimeError(f"MyMemory пустой ответ: {data.get('responseDetails')}")
        translated.append(piece)
    return " ".join(translated).strip()


def translate_to_ru(text):
    """Перевод на русский через бесплатные endpoint'ы Google.
    Бесплатные эндпоинты периодически отвечают 429/403 на запросы из
    облака (в т.ч. с IP GitHub Actions), поэтому пробуем несколько раз
    и через разные адреса."""
    if not text.strip():
        return text

    if _is_mostly_russian(text):
        return text

    # текст длиннее ~4500 символов Google обрезает — режем сами
    last_error = None
    if len(text) > 4000:
        text = text[:4000].rsplit(" ", 1)[0]

    # Порядок важен: Google стабильно отвечает 429 с IP GitHub Actions,
    # поэтому основным сделан MyMemory, а Google остался запасным.
    # Так экономятся ~10 секунд на каждой заведомо провальной попытке.
    try:
        result = _translate_mymemory(text)
        if result:
            return result
    except Exception as e:
        last_error = e
        print(f"MyMemory не ответил ({e}), пробую Google…")

    endpoints = [
        "https://translate.googleapis.com/translate_a/single",
        "https://clients5.google.com/translate_a/t",
    ]
    for endpoint in endpoints:
        try:
            result = _try_translate_once(text, endpoint)
            if result:
                return result
        except Exception as e:
            last_error = e

    print(f"!!! ПЕРЕВОД НЕ УДАЛСЯ ни через MyMemory, ни через Google — "
          f"публикую оригинал. Причина: {last_error}")
    return text


# ---------- Сбор кандидатов ----------

def entry_age_hours(entry):
    """Возраст записи в часах. None — если дату определить не удалось."""
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            try:
                published = datetime.datetime(*t[:6], tzinfo=datetime.timezone.utc)
                return (datetime.datetime.now(datetime.timezone.utc)
                        - published).total_seconds() / 3600
            except Exception:
                continue
    return None


def fetch_rss_candidates():
    candidates = []
    feeds = list(FEEDS)
    if TOPIC == "tech":
        feeds += TECH_FEEDS
    too_old = 0
    no_date = 0
    for feed_url in feeds:
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

            # Отсекаем старые записи: ленты отдают материалы за несколько
            # дней, и без этого в канал попадает позавчерашнее.
            age = entry_age_hours(entry)
            if age is None:
                # Дату определить не удалось — публиковать опасно, именно
                # такие записи и лезли старьём. Лучше пропустить новость,
                # чем выдать недельную давность.
                no_date += 1
                continue
            if age > MAX_AGE_HOURS:
                too_old += 1
                continue

            candidates.append({
                "cid": entry_id(link),
                "title": title,
                "summary": clean_html(entry.get("summary", "") or entry.get("description", "")),
                "link": link,
                "source": source_name,
                "image_url": get_image_url(entry),
                "age_hours": age,
            })
    if too_old or no_date:
        print(f"Отброшено: устаревших (старше {MAX_AGE_HOURS} ч) — {too_old}, "
              f"без даты публикации — {no_date}")
    # свежие — первыми
    candidates.sort(key=lambda c: c.get("age_hours", 999))
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
- importance: "top" если новость заметна для рынка: решения ФРС/центробанков
  и макростатистика, регуляторные решения, иски и расследования, ощутимые
  движения цены BTC/ETH/индексов, взломы и потери от $1 млн, сделки,
  привлечения и покупки от $10 млн, листинги/делистинги на крупных биржах,
  движения китов и институционалов, банкротства, заявления заметных фигур
  (политики, главы ЦБ, руководители крупных компаний и бирж).
  "normal" — для всего остального: технический анализ и ценовые прогнозы,
  колонки и мнения, "куда пойдёт монета X", промо и спонсорские материалы,
  подборки, гайды и образовательный контент, мелкие обновления протоколов.
  Ориентир: из списка кандидатов примерно четверть-треть обычно тянет на "top".
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
    # seen — для быстрой проверки, posted_order — сохраняет порядок
    posted_order = list(state["posted_ids"])
    posted_ids = set(posted_order)

    def mark_seen(cid):
        if cid not in posted_ids:
            posted_ids.add(cid)
            posted_order.append(cid)

    history = state["history"]

    raw_candidates = fetch_rss_candidates() + fetch_polymarket_candidates()
    # первый, дешёвый слой дедупа — по точной ссылке/id, без всякого ИИ
    candidates = [c for c in raw_candidates if c["cid"] not in posted_ids]

    # Отбор по теме запуска: crypto или macro. Так две параллельные
    # задачи не публикуют одно и то же и делят ленту по смыслу.
    if TOPIC in ("crypto", "macro", "tech"):
        before = len(candidates)
        candidates = [c for c in candidates if detect_topic(c) == TOPIC]
        print(f"Тема запуска: {TOPIC}. Подходящих новостей: "
              f"{len(candidates)} из {before}")

    if not candidates:
        print("Новых кандидатов нет.")
        return

    # --- Локальный дедуп (работает всегда, даже без Gemini) ---
    # 1) против уже опубликованного за последние часы
    history_tokens = [
        (h.get("message_id"), significant_tokens(f"{h.get('title','')}"))
        for h in history
    ]
    deduped = []
    dup_local = 0
    for c in candidates:
        c["tokens"] = significant_tokens(c["title"])
        same_as = None
        for mid, htok in history_tokens:
            if is_same_story(c["tokens"], htok):
                same_as = mid
                break
        if same_as is not None:
            dup_local += 1
            mark_seen(c["cid"])   # больше к этой новости не возвращаемся
            continue
        # 2) против уже отобранных в этой же пачке (разные СМИ, одно событие)
        clash = False
        for kept in deduped:
            if is_same_story(c["tokens"], kept["tokens"]):
                clash = True
                break
        if clash:
            dup_local += 1
            mark_seen(c["cid"])
            continue
        deduped.append(c)

    if dup_local:
        print(f"Локальный дедуп отсеял повторов: {dup_local}")
    candidates = deduped

    classifications = classify_batch(candidates, history)

    if classifications:
        print(f"Gemini обработал кандидатов: {len(classifications)}")
    elif not GEMINI_API_KEY:
        print("!!! GEMINI_API_KEY не задан — работаю в резервном режиме "
              "(без смыслового дедупа, апдейтов и точного ранжирования). "
              "Добавьте секрет GEMINI_API_KEY в настройках репозитория.")
    else:
        print("!!! Gemini не ответил — работаю в резервном режиме.")

    scored = []
    rejected_ids = []
    stats = {"не по теме": 0, "Украина": 0, "дубль": 0, "не топ": 0,
             "резерв: прошло": 0, "резерв: не топ": 0, "резерв: отсеяно": 0}
    for c in candidates:
        cls = (classifications or {}).get(c["cid"])

        if cls is None:
            # Gemini недоступен (нет ключа/сбой/лимит). Раньше бот тут
            # молчал — теперь работает по грубой эвристике на ключевых
            # словах, чтобы канал не оставался пустым.
            fb = fallback_classify(c)
            if fb is None:
                stats["резерв: отсеяно"] += 1
                rejected_ids.append(c["cid"])
                continue
            if TOP_ONLY and fb["importance"] != "top":
                stats["резерв: не топ"] += 1
                rejected_ids.append(c["cid"])
                continue
            stats["резерв: прошло"] += 1
            scored.append((c, fb))
            continue

        # Кандидат прошёл через Gemini — что бы он ни решил, второй раз
        # прогонять эту же новость не нужно.
        rejected_ids.append(c["cid"])

        if not cls.get("relevant", True):
            stats["не по теме"] += 1
            continue
        if cls.get("mentions_ukraine"):
            stats["Украина"] += 1
            continue
        if cls.get("duplicate_of_message_id"):
            stats["дубль"] += 1
            continue

        is_top = cls.get("importance") == "top"
        is_update = bool(cls.get("update_of_message_id"))

        # В строгом режиме публикуем только топ. Исключение — уточнения к
        # уже опубликованным постам: их пропускаем дальше, иначе история
        # на канале останется оборванной.
        if TOP_ONLY and not is_top and not is_update:
            stats["не топ"] += 1
            continue

        for name in cls.get("flagged_entities", []) or []:
            if name not in FLAGGED_ENTITIES:
                print(f"[проверьте вручную] Gemini предполагает иноагента/запрет: {name}")

        scored.append((c, cls))

    # топовые (⚡️) — первыми
    scored.sort(key=lambda pair: 0 if pair[1].get("importance") == "top" else 1)

    print(f"Кандидатов всего: {len(candidates)}, прошло фильтр: {len(scored)}")
    print(f"Отсеяно: {stats}")

    # Аварийный клапан: фильтры отсеяли вообще всё, а новости были.
    # Лучше опубликовать пару менее громких новостей, чем оставить
    # канал молчать целый час.
    if not scored and candidates:
        print("Ничего не прошло фильтр — включаю аварийный отбор")
        ranked = []
        for c in candidates:
            text = f"{c['title']} {c['summary']}".lower()
            if any(kw in text for kw in HARD_SKIP_KEYWORDS):
                continue
            if any(kw in text for kw in FALLBACK_SKIP_KEYWORDS):
                continue
            hits = sum(1 for kw in FALLBACK_TOP_KEYWORDS if kw in text)
            if hits:
                ranked.append((hits, c))
        ranked.sort(key=lambda x: -x[0])
        for hits, c in ranked[:MAX_POSTS_PER_RUN]:  # обрежется квотой ниже
            scored.append((c, {"importance": "normal",
                               "update_of_message_id": None}))
        print(f"Аварийный отбор дал постов: {len(scored)}")

    # --- Сколько постов публиковать в этот запуск ---
    # Планировщик GitHub на бесплатном тарифе регулярно пропускает
    # запуски. Поэтому ориентируемся не на "1 пост за запуск", а на
    # время, прошедшее с последней публикации: сколько задолжали —
    # столько и публикуем (в пределах MAX_POSTS_PER_RUN).
    now = datetime.datetime.now(datetime.timezone.utc)
    last_ts = state.get("last_post_ts")
    interval = 60 / max(POSTS_PER_HOUR, 1)      # минут на один пост
    quota = 1
    if last_ts:
        try:
            elapsed_min = (now - datetime.datetime.fromisoformat(last_ts)).total_seconds() / 60
            # Допуск: метки расписания не совпадают идеально с интервалом.
            # Без него пост в :10 при интервале 30 мин блокировал бы метки
            # :32 (22 мин) и :38 (28 мин), и вместо 2 постов выходил бы 1.
            quota = int((elapsed_min * 1.35) // interval)
            if quota < 1:
                # Интервал ещё не выдержан. Такое бывает штатно: расписаний
                # два (для надёжности), и второе срабатывает вскоре после
                # первого. Просто выходим, чтобы не превысить темп.
                print(f"С последней публикации прошло {int(elapsed_min)} мин "
                      f"(нужно {int(interval)}) — пропускаю запуск")
                state["posted_ids"] = posted_order
                state["history"] = history
                save_state(state)
                return
            if quota > 1:
                print(f"С последней публикации прошло {int(elapsed_min)} мин — "
                      f"наверстываю {min(quota, MAX_POSTS_PER_RUN)} постов")
        except Exception as e:
            print(f"Не смог разобрать last_post_ts ({e}), публикую один пост")
            quota = 1
    quota = min(quota, MAX_POSTS_PER_RUN)

    # жёсткий потолок на количество постов за запуск
    if len(scored) > quota:
        print(f"Кандидатов после фильтра: {len(scored)}, публикую топ-{quota}")
        deferred = scored[quota:]
        # отложенные на следующий запуск — снимаем с них отметку "обработан"
        deferred_ids = {c["cid"] for c, _ in deferred}
        rejected_ids = [rid for rid in rejected_ids if rid not in deferred_ids]
        scored = scored[:quota]

    # всё, что Gemini отсеял, помечаем как обработанное — чтобы не гонять
    # одни и те же новости через Gemini на каждом запуске
    for _rid in rejected_ids:
        mark_seen(_rid)

    # Небольшая случайная задержка, чтобы посты не выходили строго
    # по минутам расписания. Запуск при этом остаётся коротким —
    # длинные job'ы срывались из-за задержек расписания GitHub.
    offsets = []
    if scored:
        offsets = [random.randint(0, JITTER_MAX_MINUTES)]
        for i in range(1, len(scored)):
            offsets.append(offsets[-1] + random.randint(1, 3))
    print(f"План публикаций (минут от старта): {offsets}")

    sent = 0
    elapsed = 0  # сколько минут уже прошло с начала запуска
    for (c, cls), offset in zip(scored, offsets):
        wait_minutes = offset - elapsed
        if wait_minutes > 0:
            print(f"Жду {wait_minutes} мин до следующей публикации…")
            time.sleep(wait_minutes * 60)
            elapsed = offset

        is_top = cls.get("importance") == "top"
        text = build_post_text(c, is_top)

        reply_to = None
        update_id = cls.get("update_of_message_id")
        if update_id:
            reply_to = update_id
            text = "🔄 Уточнение по ранее опубликованной новости:\n\n" + text

        hashtags = "#AlphaFeedru"
        text = f"{text}\n\n{hashtags}"

        age = c.get("age_hours")
        age_str = f"{age:.1f} ч назад" if isinstance(age, (int, float)) else "возраст неизвестен"
        print(f"Публикую ({age_str}): {c['title'][:70]}")

        message_id = send_to_telegram(text, c.get("image_url"), reply_to)
        if message_id:
            mark_seen(c["cid"])
            history.append({
                "cid": c["cid"],
                "message_id": message_id,
                "title": c["title"],
                "summary": c["summary"][:300],
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            })
            sent += 1

            # сохраняем состояние после КАЖДОГО поста: запуск длится почти
            # час, и если он оборвётся посреди — уже опубликованное не
            # уйдёт в канал повторно на следующем часу
            state["posted_ids"] = posted_order
            state["history"] = history
            state["last_post_ts"] = datetime.datetime.now(
                datetime.timezone.utc).isoformat()
            save_state(state)

    state["posted_ids"] = posted_order
    state["history"] = history
    save_state(state)
    print(f"Готово. Отправлено новых постов: {sent}")


if __name__ == "__main__":
    main()