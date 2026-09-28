#!/bin/bash
# Зерно — запуск проверки работоспособности одной командой.
# Ничего не публикует и не меняет — только читает и проверяет.
cd /opt/zerno-bot || exit 1
set -a
source /opt/zerno-bot/.env
set +a
/opt/zerno-bot/venv/bin/python -u /opt/zerno-bot/healthcheck.py
