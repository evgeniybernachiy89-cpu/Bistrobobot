#!/bin/bash
# Установка новостного бота "Зерно" на чистый VPS (Ubuntu 22.04/24.04).
# Запускается ОДНОЙ командой, всё остальное делает сам.
set -e

echo "=============================================="
echo "  Установка новостного бота «Зерно»"
echo "=============================================="
echo

# --- 1. Спрашиваем секреты ---
read -p "Вставьте токен Telegram-бота: " TG_TOKEN
read -p "Вставьте chat_id канала (с минусом, если канал): " TG_CHAT
read -p "Ключ Gemini (просто Enter, если нет): " GEM_KEY
read -p "Ссылка на репозиторий GitHub (https://github.com/USER/REPO.git): " REPO_URL

BOT_DIR="/opt/zerno-bot"

# --- 2. Системные пакеты ---
echo
echo ">>> Ставлю Python и git..."
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv git curl >/dev/null

# --- 3. Забираем код ---
echo ">>> Скачиваю код бота..."
rm -rf "$BOT_DIR"
git clone --quiet "$REPO_URL" "$BOT_DIR"
cd "$BOT_DIR"

# --- 4. Виртуальное окружение и зависимости ---
echo ">>> Устанавливаю зависимости..."
python3 -m venv venv
./venv/bin/pip install --quiet --upgrade pip
./venv/bin/pip install --quiet feedparser requests

# --- 5. Файл с настройками ---
echo ">>> Сохраняю настройки..."
cat > "$BOT_DIR/.env" << ENVEOF
TELEGRAM_BOT_TOKEN=$TG_TOKEN
TELEGRAM_CHAT_ID=$TG_CHAT
GEMINI_API_KEY=$GEM_KEY
TOPIC=rotate
TOPIC_ROTATION=crypto,macro,crypto,tech
MIN_INTERVAL_MINUTES=14
STATE_FILE=$BOT_DIR/posted_state.json
MAX_POSTS_PER_RUN=1
MAX_AGE_HOURS=12
TOP_ONLY=true
JITTER_MAX_MINUTES=3
ENVEOF
chmod 600 "$BOT_DIR/.env"

# --- 6. Скрипт запуска ---
cat > "$BOT_DIR/run.sh" << 'RUNEOF'
#!/bin/bash
cd /opt/zerno-bot
set -a
source /opt/zerno-bot/.env
set +a
/opt/zerno-bot/venv/bin/python -u /opt/zerno-bot/fetch_and_post.py \
    >> /opt/zerno-bot/bot.log 2>&1
# лог не должен разрастаться
tail -n 3000 /opt/zerno-bot/bot.log > /opt/zerno-bot/bot.log.tmp \
    && mv /opt/zerno-bot/bot.log.tmp /opt/zerno-bot/bot.log
RUNEOF
chmod +x "$BOT_DIR/run.sh"

# --- 7. Состояние ---
if [ ! -f "$BOT_DIR/posted_state.json" ]; then
  echo '{"posted_ids": [], "history": [], "last_post_ts": null, "rotation_index": 0}' \
      > "$BOT_DIR/posted_state.json"
fi

# --- 8. Расписание ---
echo ">>> Настраиваю расписание (каждые 5 минут)..."
crontab -l 2>/dev/null | grep -v 'zerno-bot' > /tmp/cron.tmp || true
echo "*/5 * * * * /opt/zerno-bot/run.sh" >> /tmp/cron.tmp
crontab /tmp/cron.tmp
rm -f /tmp/cron.tmp

# --- 9. Проверочный запуск ---
echo
echo ">>> Пробный запуск..."
"$BOT_DIR/run.sh" || true
echo
echo "----- последние строки лога -----"
tail -n 20 "$BOT_DIR/bot.log" 2>/dev/null || echo "(лог пуст)"
echo "---------------------------------"
echo
echo "=============================================="
echo "  ГОТОВО. Бот работает и запускается сам."
echo
echo "  Посмотреть лог:      tail -f /opt/zerno-bot/bot.log"
echo "  Запустить вручную:   /opt/zerno-bot/run.sh"
echo "  Изменить настройки:  nano /opt/zerno-bot/.env"
echo "  Обновить код:        cd /opt/zerno-bot && git pull"
echo "=============================================="
