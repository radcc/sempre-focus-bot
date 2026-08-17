#!/usr/bin/env bash
# Разовая настройка сервера. Запускается на сервере из /root/sempre-deploy,
# где уже лежат bot.py, requirements.txt, sempre-bot.service и .env.
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

echo "== пакеты =="
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip ufw > /dev/null

echo "== пользователь sempre =="
id -u sempre &>/dev/null || useradd --system --home /opt/sempre-bot --shell /usr/sbin/nologin sempre

echo "== файлы =="
mkdir -p /opt/sempre-bot
install -m 644 bot.py requirements.txt /opt/sempre-bot/
install -m 600 .env /opt/sempre-bot/.env

echo "== venv =="
python3 -m venv /opt/sempre-bot/venv
/opt/sempre-bot/venv/bin/pip install --quiet --upgrade pip
/opt/sempre-bot/venv/bin/pip install --quiet -r /opt/sempre-bot/requirements.txt

chown -R sempre:sempre /opt/sempre-bot

echo "== systemd =="
install -m 644 sempre-bot.service /etc/systemd/system/sempre-bot.service
systemctl daemon-reload
systemctl enable sempre-bot
systemctl restart sempre-bot

echo "== ufw: только SSH =="
ufw allow OpenSSH > /dev/null
ufw --force enable > /dev/null
ufw status

sleep 3
systemctl --no-pager status sempre-bot || true
