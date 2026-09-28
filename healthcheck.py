#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Зерно — проверка работоспособности (healthcheck)
--------------------------------------------------
Одна команда для самопроверки всей системы: бот, переводчики, Telegram,
VK, cron, диск, код на сервере.

ВАЖНО: этот скрипт ТОЛЬКО ЧИТАЕТ и делает ТОЛЬКО безопасные, "на чтение"
запросы к Telegram/VK (getMe, getChat, users.get, groups.getById).
Он НИКОГДА не отправляет посты (sendMessage/sendPhoto/sendVoice/wall.post)
и не меняет ни одного файла состояния (posted_state.json и т.д.).
Секреты (токены) нигде не печатаются — только "задан" / "не задан".

Запуск на сервере (после git pull):
    /opt/zerno-bot/healthcheck.sh
"""

import os
import sys
import json
import shutil
import subprocess
import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

OK, WARN, FAIL, INFO = "✅", "⚠️ ", "❌", "ℹ️ "

_results = []  # (level, section) — для итоговой сводки


def report(level, section, message):
    _results.append((level, section))
    print(f"{level} [{section}] {message}")


def header(title):
    print(f"\n--- {title} ---")


def safe_int_env(name, default):
    try:
        return float(os.environ.get(name, default))
    except Exception:
        return float(default)


def short_error(e):
    """Короткое, понятное описание ошибки вместо длинного стека urllib3."""
    try:
        import requests
        if isinstance(e, requests.exceptions.Timeout):
            return "сервер не ответил вовремя (таймаут)"
        if isinstance(e, requests.exceptions.SSLError):
            return "ошибка SSL-соединения"
        if isinstance(e, requests.exceptions.ConnectionError):
            return "не удалось подключиться (нет сети до сервиса или сервис недоступен)"
        if isinstance(e, requests.exceptions.HTTPError):
            return f"сервер ответил ошибкой: {e}"
    except Exception:
        pass
    text = str(e)
    return text[:150] + ("…" if len(text) > 150 else "")


# ============================================================
# 0. Заголовок, время
# ============================================================

print("=" * 60)
print("  Зерно — проверка работоспособности")
now_utc = datetime.datetime.now(datetime.timezone.utc)
print(f"  Запуск: {now_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC")
try:
    from zoneinfo import ZoneInfo
    now_msk = now_utc.astimezone(ZoneInfo("Europe/Moscow"))
    print(f"  Время в Москве: {now_msk.strftime('%Y-%m-%d %H:%M:%S')}")
except Exception:
    pass
print("  Ничего не публикует и не меняет — только проверяет.")
print("=" * 60)


# ============================================================
# 1. Основной код бота: удаётся ли его вообще подключить
# ============================================================

header("1. Код бота")

core_ok = False
try:
    import fetch_and_post as core
    core_ok = True
    report(OK, "код", "fetch_and_post.py читается и импортируется без ошибок")
except Exception as e:
    report(FAIL, "код", f"не смог загрузить fetch_and_post.py: {e}")
    print("    Дальше многие проверки будут пропущены — сначала почини это.")


# ============================================================
# 2. Переменные окружения (.env) — только наличие, не значения
# ============================================================

header("2. Настройки (.env)")

required_vars = {
    "TELEGRAM_BOT_TOKEN": "без него бот вообще не запустится",
    "TELEGRAM_CHAT_ID": "без него бот не знает, куда публиковать",
}
optional_vars = {
    "GEMINI_API_KEY": "нет — бот работает в резервном режиме "
                       "(без ИИ-дедупа и ранжирования, на ключевых словах)",
    "VK_ACCESS_TOKEN": "нет — автопостинг в VK выключен",
    "VK_GROUP_ID": "нет — автопостинг в VK выключен",
    "MYMEMORY_EMAIL": "нет — общий бесплатный лимит переводчика MyMemory "
                       "(5000 слов/сутки вместо 50000)",
}

for name, why_bad in required_vars.items():
    val = os.environ.get(name)
    if val and val.strip():
        report(OK, "настройки", f"{name} задан")
    else:
        report(FAIL, "настройки", f"{name} НЕ задан — {why_bad}")

for name, note in optional_vars.items():
    val = os.environ.get(name)
    if val and val.strip():
        report(OK, "настройки", f"{name} задан")
    else:
        report(INFO, "настройки", f"{name} {note}")

vk_configured = bool(os.environ.get("VK_ACCESS_TOKEN")) and bool(os.environ.get("VK_GROUP_ID"))

if core_ok:
    print(f"    Текущий режим: TOPIC={os.environ.get('TOPIC', '?')}, "
          f"ротация={os.environ.get('TOPIC_ROTATION', '?')}, "
          f"MIN_INTERVAL_MINUTES={os.environ.get('MIN_INTERVAL_MINUTES', '?')}, "
          f"MAX_AGE_HOURS={os.environ.get('MAX_AGE_HOURS', '?')}, "
          f"TOP_ONLY={os.environ.get('TOP_ONLY', '?')}")


# ============================================================
# 3. Тихие часы
# ============================================================

header("3. Тихие часы")

if core_ok:
    try:
        quiet_now = core.in_quiet_hours()
        window = f"{core.QUIET_HOURS_START}–{core.QUIET_HOURS_END} {core.QUIET_HOURS_TZ}"
        if quiet_now:
            report(INFO, "тихие часы", f"сейчас ИДУТ тихие часы ({window}) — "
                                        f"публикации приостановлены, это нормально")
        else:
            report(OK, "тихие часы", f"сейчас не тихие часы (окно: {window})")
    except Exception as e:
        report(WARN, "тихие часы", f"не смог проверить: {e}")
        quiet_now = False
else:
    quiet_now = False
    report(WARN, "тихие часы", "пропущено (не загрузился код бота)")


# ============================================================
# 4. Состояние публикаций: когда был последний пост
# ============================================================

header("4. Активность публикаций")


def check_state_file(path, ts_key, label, expected_interval_min, currently_quiet):
    """Смотрит last_post_ts/last_ts в файле состояния и оценивает,
    не пропала ли публикация надолго."""
    if not os.path.exists(path):
        report(INFO, label, f"файл {os.path.basename(path)} не найден — "
                             f"похоже, ещё ни разу не запускался на этом сервере")
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        report(FAIL, label, f"файл {os.path.basename(path)} повреждён, не читается: {e}")
        return

    ts = data.get(ts_key)
    posted_count = len(data.get("posted_ids", []) or [])
    print(f"    Всего записано опубликованных id: {posted_count}")

    if not ts:
        report(WARN, label, "публикаций пока не было (last_post_ts пуст)")
        return

    try:
        last_dt = datetime.datetime.fromisoformat(ts)
        elapsed_min = (now_utc - last_dt).total_seconds() / 60
    except Exception as e:
        report(WARN, label, f"не смог разобрать время последнего поста: {e}")
        return

    warn_th = max(expected_interval_min * 4, 60)
    fail_th = 300 if not currently_quiet else 24 * 60  # тихие часы дают запас

    if elapsed_min < 0:
        report(WARN, label, "время последнего поста в будущем — часы сервера сбиты?")
    elif elapsed_min <= warn_th:
        report(OK, label, f"последняя публикация {elapsed_min:.0f} мин назад "
                           f"(ожидание ~{expected_interval_min:.0f} мин)")
    elif elapsed_min <= fail_th:
        extra = " (сейчас как раз тихие часы — это может объяснять паузу)" if currently_quiet else \
                " (могло просто не найтись подходящих новостей)"
        report(WARN, label, f"последняя публикация {elapsed_min:.0f} мин назад — "
                             f"дольше обычного, но не критично{extra}")
    else:
        report(FAIL, label, f"последняя публикация {elapsed_min/60:.1f} ч назад — "
                             f"подозрительно долгое молчание, проверь лог")


if core_ok:
    state_file = os.environ.get("STATE_FILE", os.path.join(BASE_DIR, "posted_state.json"))
    check_state_file(state_file, "last_post_ts", "Telegram-бот",
                      safe_int_env("MIN_INTERVAL_MINUTES", 14), quiet_now)

    if vk_configured:
        vk_state_file = os.environ.get("VK_STATE_FILE", os.path.join(BASE_DIR, "posted_vk.json"))
        check_state_file(vk_state_file, "last_ts", "VK",
                          safe_int_env("VK_INTERVAL_MINUTES", 20), quiet_now)
    else:
        report(INFO, "VK", "VK не настроен — пропускаю проверку публикаций VK")

    price_state_file = os.environ.get("PRICE_STATE_FILE", os.path.join(BASE_DIR, "price_state.json"))
    if os.path.exists(price_state_file):
        check_state_file(price_state_file, "last_ts", "голосовые цены",
                          safe_int_env("PRICE_INTERVAL_MINUTES", 30), quiet_now)
    else:
        report(INFO, "голосовые цены", "price_state.json не найден — "
                                        "голосовой комментатор не настроен или ещё не запускался")
else:
    report(WARN, "публикации", "пропущено (не загрузился код бота)")

# Лог: если файл не обновлялся дольше нескольких циклов cron — cron,
# скорее всего, вообще не запускает бота (это отдельная проблема от
# "запускается, но нечего публиковать").
log_path = os.path.join(BASE_DIR, "bot.log")
if os.path.exists(log_path):
    age_min = (datetime.datetime.now().timestamp() - os.path.getmtime(log_path)) / 60
    if age_min <= 15:
        report(OK, "bot.log", f"обновлялся {age_min:.0f} мин назад — cron запускает бота")
    elif age_min <= 60:
        report(WARN, "bot.log", f"не обновлялся {age_min:.0f} мин — "
                                 f"проверь, что cron ещё работает (crontab -l)")
    else:
        report(FAIL, "bot.log", f"не обновлялся {age_min/60:.1f} ч — "
                                 f"похоже, cron не запускает run.sh вообще")
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            tail_lines = f.readlines()[-80:]
        errors = [ln.strip() for ln in tail_lines if "Traceback" in ln or "!!! " in ln]
        if errors:
            report(WARN, "bot.log", f"в последних строках лога есть тревожные записи "
                                     f"({len(errors)} шт.), например: {errors[-1][:120]}")
    except Exception:
        pass
else:
    report(INFO, "bot.log", "файл лога не найден рядом со скриптом")


# ============================================================
# 5. Cron
# ============================================================

header("5. Расписание (cron)")

try:
    proc = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10)
    if proc.returncode != 0:
        report(WARN, "cron", "команда crontab -l не вернула список "
                              "(возможно, расписание вообще не настроено)")
    else:
        lines = [ln for ln in proc.stdout.splitlines()
                 if ln.strip() and not ln.strip().startswith("#")]
        bot_lines = [ln for ln in lines if "run.sh" in ln or "fetch_and_post" in ln]
        vk_lines = [ln for ln in lines if "vk" in ln.lower()]
        price_lines = [ln for ln in lines if "price" in ln.lower()]

        if bot_lines:
            report(OK, "cron", f"есть запись для основного бота: {bot_lines[0].strip()}")
        else:
            report(FAIL, "cron", "не нашёл запись для run.sh / fetch_and_post.py — "
                                  "бот не будет запускаться сам")

        if vk_configured and not vk_lines:
            report(WARN, "cron", "VK настроен в .env, но в cron нет отдельной записи "
                                  "для VK — автопостинг в VK работать не будет")
        elif vk_lines:
            report(OK, "cron", f"есть запись для VK: {vk_lines[0].strip()}")

        if os.path.exists(os.path.join(BASE_DIR, "price_commentary.py")) and not price_lines:
            report(INFO, "cron", "price_commentary.py есть на сервере, но в cron "
                                  "для него записи не видно (если голосовые посты не нужны — это нормально)")
        elif price_lines:
            report(OK, "cron", f"есть запись для голосовых цен: {price_lines[0].strip()}")
except FileNotFoundError:
    report(WARN, "cron", "команда crontab не найдена на сервере")
except Exception as e:
    report(WARN, "cron", f"не смог проверить: {e}")


# ============================================================
# 6. Код на сервере (git) — не свежий ли он
# ============================================================

header("6. Версия кода на сервере")

git_dir = os.path.join(BASE_DIR, ".git")
if not os.path.isdir(git_dir):
    report(INFO, "git", "это не git-репозиторий — пропускаю проверку версии")
else:
    try:
        def git(*args):
            r = subprocess.run(["git", "-C", BASE_DIR] + list(args),
                                capture_output=True, text=True, timeout=10)
            return r.stdout.strip(), r.returncode

        commit, rc1 = git("rev-parse", "--short", "HEAD")
        date_str, rc2 = git("log", "-1", "--format=%cd", "--date=format:%Y-%m-%d %H:%M")
        if rc1 == 0:
            report(OK, "git", f"текущий commit {commit}, от {date_str or '?'}")
        else:
            report(WARN, "git", "не смог определить текущий commit")

        dirty, rc3 = git("status", "--porcelain")
        if rc3 == 0 and dirty:
            changed = len(dirty.splitlines())
            report(WARN, "git", f"на сервере есть {changed} незакоммиченных изменений — "
                                 f"при следующем git pull они могут потеряться "
                                 f"или вызвать конфликт")
        elif rc3 == 0:
            report(OK, "git", "нет локальных изменений, код совпадает с последним коммитом")

        print("    Напоминание: если недавно правили код на GitHub — сделайте "
              "`git pull` и запустите проверку заново, чтобы свериться.")
    except Exception as e:
        report(WARN, "git", f"не смог проверить: {e}")

    workflows_dir = os.path.join(BASE_DIR, ".github", "workflows")
    if os.path.isdir(workflows_dir):
        ymls = [f for f in os.listdir(workflows_dir) if f.endswith((".yml", ".yaml"))]
        if ymls:
            report(WARN, "GitHub Actions",
                   f"в репозитории есть файлы автоматизации: {', '.join(ymls)}. "
                   f"Если публикуете через VPS — убедитесь, что на GitHub во вкладке "
                   f"Actions они отключены (Disable workflow), иначе есть риск задвоения постов")


# ============================================================
# 7. Telegram — проверка на чтение (без публикации)
# ============================================================

header("7. Telegram (проверка без отправки)")

tg_token = os.environ.get("TELEGRAM_BOT_TOKEN")
tg_chat = os.environ.get("TELEGRAM_CHAT_ID")

if not tg_token:
    report(FAIL, "Telegram", "пропущено — TELEGRAM_BOT_TOKEN не задан")
else:
    try:
        import requests
        r = requests.get(f"https://api.telegram.org/bot{tg_token}/getMe", timeout=10)
        data = r.json()
        if data.get("ok"):
            uname = data["result"].get("username", "?")
            report(OK, "Telegram", f"токен рабочий, бот: @{uname}")
        else:
            report(FAIL, "Telegram", f"токен не принят Telegram: {data.get('description', r.text)[:150]}")
    except Exception as e:
        report(FAIL, "Telegram", f"нет связи с Telegram: {short_error(e)}")

    if not tg_chat:
        report(FAIL, "Telegram", "пропущено — TELEGRAM_CHAT_ID не задан")
    else:
        try:
            import requests
            r = requests.get(f"https://api.telegram.org/bot{tg_token}/getChat",
                              params={"chat_id": tg_chat}, timeout=10)
            data = r.json()
            if data.get("ok"):
                title = data["result"].get("title") or data["result"].get("username") or tg_chat
                report(OK, "Telegram", f"бот видит канал/чат: «{title}»")
            else:
                report(FAIL, "Telegram", f"бот не видит chat_id={tg_chat}: "
                                          f"{data.get('description', r.text)[:150]} "
                                          f"(проверьте id и что бот добавлен админом)")
        except Exception as e:
            report(FAIL, "Telegram", f"нет связи с Telegram: {short_error(e)}")


# ============================================================
# 8. VK — проверка на чтение (без публикации)
# ============================================================

header("8. VK (проверка без отправки)")

if not vk_configured:
    report(INFO, "VK", "не настроен (VK_ACCESS_TOKEN/VK_GROUP_ID не заданы) — пропускаю")
else:
    vk_token = os.environ.get("VK_ACCESS_TOKEN")
    vk_group = os.environ.get("VK_GROUP_ID")
    vk_v = os.environ.get("VK_API_VERSION", "5.199")
    try:
        import requests
        r = requests.get("https://api.vk.com/method/users.get",
                          params={"access_token": vk_token, "v": vk_v}, timeout=10)
        data = r.json()
        if "error" in data:
            err = data["error"]
            report(FAIL, "VK", f"токен не принят: {err.get('error_msg', '?')}")
        else:
            u = (data.get("response") or [{}])[0]
            name = f"{u.get('first_name', '')} {u.get('last_name', '')}".strip() or "?"
            report(OK, "VK", f"токен рабочий, аккаунт: {name}")
    except Exception as e:
        report(FAIL, "VK", f"нет связи с VK: {short_error(e)}")

    try:
        import requests
        r = requests.get("https://api.vk.com/method/groups.getById",
                          params={"group_id": vk_group, "access_token": vk_token, "v": vk_v},
                          timeout=10)
        data = r.json()
        if "error" in data:
            err = data["error"]
            report(FAIL, "VK", f"не вижу группу id={vk_group}: {err.get('error_msg', '?')} "
                                f"(проверьте VK_GROUP_ID — число БЕЗ минуса)")
        else:
            groups = data.get("response", {})
            items = groups.get("groups", groups) if isinstance(groups, dict) else groups
            gname = (items[0].get("name") if items else None) or "?"
            report(OK, "VK", f"вижу группу: «{gname}»")
    except Exception as e:
        report(FAIL, "VK", f"нет связи с VK: {short_error(e)}")


# ============================================================
# 9. Переводчики — по одному, как в проде
# ============================================================

header("9. Переводчики (MyMemory → Lingva → Google)")

if core_ok:
    test_text = "Bitcoin price rises after new report."

    try:
        result = core._translate_mymemory(test_text)
        if result:
            report(OK, "MyMemory", f"работает: «{result[:60]}»")
        else:
            report(FAIL, "MyMemory", "вернул пустой ответ")
    except Exception as e:
        report(FAIL, "MyMemory", f"не отвечает: {short_error(e)}")

    try:
        result = core._translate_lingva(test_text)
        if result:
            report(OK, "Lingva", f"работает: «{result[:60]}»")
        else:
            report(FAIL, "Lingva", "вернула пустой ответ (все зеркала недоступны)")
    except Exception as e:
        report(FAIL, "Lingva", f"не отвечает: {short_error(e)}")

    try:
        result = core._try_translate_once(test_text, "https://translate.googleapis.com/translate_a/single")
        if result:
            report(OK, "Google", f"работает: «{result[:60]}»")
        else:
            report(WARN, "Google", "вернул пустой ответ")
    except Exception as e:
        report(WARN, "Google", f"не отвечает (это не страшно, он и так последний "
                                f"в очереди): {short_error(e)}")

    print("    Напоминание: боту достаточно, чтобы работал ХОТЯ БЫ ОДИН из трёх —")
    print("    если выше есть хотя бы один ✅, переводы публиковаться будут.")
else:
    report(WARN, "переводчики", "пропущено (не загрузился код бота)")


# ============================================================
# 10. Диск
# ============================================================

header("10. Место на диске")

try:
    total, used, free = shutil.disk_usage(BASE_DIR)
    free_gb = free / (1024 ** 3)
    free_pct = free / total * 100
    if free_gb < 0.3:
        report(FAIL, "диск", f"свободно всего {free_gb:.2f} ГБ ({free_pct:.0f}%) — "
                              f"критически мало, бот может перестать сохранять файлы")
    elif free_pct < 15:
        report(WARN, "диск", f"свободно {free_gb:.1f} ГБ ({free_pct:.0f}%) — "
                              f"стоит последить")
    else:
        report(OK, "диск", f"свободно {free_gb:.1f} ГБ ({free_pct:.0f}%)")
except Exception as e:
    report(WARN, "диск", f"не смог проверить: {e}")


# ============================================================
# 11. Голосовой комментатор — зависимости (если подключён)
# ============================================================

price_py = os.path.join(BASE_DIR, "price_commentary.py")
if os.path.exists(price_py):
    header("11. Голосовой комментатор цен")
    if shutil.which("ffmpeg"):
        report(OK, "ffmpeg", "установлен")
    else:
        report(FAIL, "ffmpeg", "не найден в системе — озвучка не сможет "
                                "сконвертировать файл в голосовое сообщение")
    try:
        import edge_tts  # noqa: F401
        report(OK, "edge-tts", "библиотека установлена")
    except Exception:
        report(FAIL, "edge-tts", "не установлена — установите: "
                                  "venv/bin/pip install edge-tts")


# ============================================================
# ИТОГ
# ============================================================

header("ИТОГ")

n_fail = sum(1 for lvl, _ in _results if lvl == FAIL)
n_warn = sum(1 for lvl, _ in _results if lvl == WARN)
n_ok = sum(1 for lvl, _ in _results if lvl == OK)

print(f"✅ ок: {n_ok}   ⚠️ предупреждений: {n_warn}   ❌ проблем: {n_fail}")

if n_fail:
    fail_sections = sorted({sec for lvl, sec in _results if lvl == FAIL})
    print(f"\n{FAIL} ЕСТЬ ПРОБЛЕМЫ, которые стоит поправить: {', '.join(fail_sections)}")
    print("   Пришлите мне вывод этой проверки целиком — разберём по порядку.")
elif n_warn:
    print(f"\n{WARN} Критичных проблем нет, но кое на что стоит посмотреть (см. выше).")
else:
    print(f"\n{OK} Всё в порядке.")

print("=" * 60)

sys.exit(1 if n_fail else 0)
