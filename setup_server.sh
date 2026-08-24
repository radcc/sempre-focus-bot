#!/usr/bin/env bash
# Настройка/обновление сервера. Запускается из /root/sempre-deploy (git-клон),
# где лежат *.py, requirements.txt, юниты и (при первой установке) .env.
# Идемпотентен: авто-деплой зовёт его при каждом обновлении кода.
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

echo "== пакеты =="
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip ufw git > /dev/null

echo "== пользователь sempre =="
id -u sempre &>/dev/null || useradd --system --home /opt/sempre-bot --shell /usr/sbin/nologin sempre

echo "== файлы =="
mkdir -p /opt/sempre-bot
install -m 644 ./*.py requirements.txt /opt/sempre-bot/
if [ -f .env ]; then
    install -m 600 .env /opt/sempre-bot/.env
elif [ ! -f /opt/sempre-bot/.env ]; then
    echo "ОШИБКА: нет .env ни в клоне, ни в /opt/sempre-bot" >&2
    exit 1
fi

echo "== venv =="
python3 -m venv /opt/sempre-bot/venv
/opt/sempre-bot/venv/bin/pip install --quiet --upgrade pip
/opt/sempre-bot/venv/bin/pip install --quiet -r /opt/sempre-bot/requirements.txt

chown -R sempre:sempre /opt/sempre-bot

echo "== systemd: бот =="
install -m 644 sempre-bot.service /etc/systemd/system/sempre-bot.service
systemctl daemon-reload
systemctl enable sempre-bot
systemctl restart sempre-bot

echo "== systemd: авто-деплой (git fetch каждые 5 минут) =="
install -m 755 sempre-autodeploy.sh /usr/local/bin/sempre-autodeploy.sh
install -m 644 sempre-autodeploy.service /etc/systemd/system/sempre-autodeploy.service
install -m 644 sempre-autodeploy.timer /etc/systemd/system/sempre-autodeploy.timer
systemctl daemon-reload
systemctl enable --now sempre-autodeploy.timer

echo "== ufw: только SSH =="
ufw allow OpenSSH > /dev/null
ufw allow 443/tcp > /dev/null || true
ufw --force enable > /dev/null
ufw status | head -5

sleep 3
systemctl --no-pager --lines=0 status sempre-bot || true
