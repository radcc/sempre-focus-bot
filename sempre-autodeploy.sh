#!/usr/bin/env bash
# Авто-деплой: если origin/main ушёл вперёд — обновиться и пересобрать.
# Защиты: маркер незавершённого прогона (kill посреди деплоя), маркер сломанного
# коммита (без вечного цикла деплой→откат), проверка на crash-loop, алерт в Telegram.
set -uo pipefail

REPO=/root/sempre-deploy
STATE=/var/lib/sempre-autodeploy
mkdir -p "$STATE"
cd "$REPO" || exit 1

notify() {
    local TOKEN CHAT
    TOKEN=$(grep -m1 '^TELEGRAM_BOT_TOKEN=' /opt/sempre-bot/.env 2>/dev/null | cut -d= -f2-)
    CHAT=$(grep -m1 '^OWNER_TELEGRAM_ID=' /opt/sempre-bot/.env 2>/dev/null | cut -d= -f2-)
    [ -n "$TOKEN" ] && [ -n "$CHAT" ] && curl -sm 10 \
        "https://api.telegram.org/bot$TOKEN/sendMessage" \
        --data-urlencode "chat_id=$CHAT" --data-urlencode "text=$1" > /dev/null || true
}

healthy() {
    systemctl is-active --quiet sempre-bot || return 1
    [ "$(systemctl show -p NRestarts --value sempre-bot)" = "0" ] || return 1
    return 0
}

# прошлый прогон был убит посреди деплоя — восстановиться на его исходный коммит
if [ -f "$STATE/in_progress" ]; then
    PREV=$(cat "$STATE/in_progress")
    echo "autodeploy: найден незавершённый прогон, восстанавливаюсь на $PREV"
    git reset --hard "$PREV" --quiet
    bash setup_server.sh || true
    rm -f "$STATE/in_progress"
fi

git fetch origin main --quiet || { echo "autodeploy: fetch не удался"; exit 0; }
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)
[ "$LOCAL" = "$REMOTE" ] && exit 0

# этот коммит уже ронял сервис — не пытаться снова, ждать следующего пуша
if [ -f "$STATE/bad_commit" ] && [ "$(cat "$STATE/bad_commit")" = "$REMOTE" ]; then
    exit 0
fi

echo "autodeploy: обновление $LOCAL -> $REMOTE"
echo "$LOCAL" > "$STATE/in_progress"
git reset --hard "$REMOTE" --quiet

if ! bash setup_server.sh; then
    echo "autodeploy: setup_server.sh упал — откат на $LOCAL"
    echo "$REMOTE" > "$STATE/bad_commit"
    git reset --hard "$LOCAL" --quiet
    bash setup_server.sh || true
    rm -f "$STATE/in_progress"
    notify "⚠️ Деплой ${REMOTE:0:7} не собрался — откатился на ${LOCAL:0:7}. Смотри journalctl -u sempre-autodeploy."
    exit 1
fi

sleep 60
if healthy; then
    echo "autodeploy: ок, работает $REMOTE"
    rm -f "$STATE/in_progress" "$STATE/bad_commit"
    exit 0
fi

echo "autodeploy: sempre-bot не поднялся — откат на $LOCAL"
echo "$REMOTE" > "$STATE/bad_commit"
git reset --hard "$LOCAL" --quiet
bash setup_server.sh || true
rm -f "$STATE/in_progress"
sleep 20
if healthy; then
    echo "autodeploy: откат успешен"
    notify "⚠️ Новый деплой ${REMOTE:0:7} не поднялся — работаю на прежней версии ${LOCAL:0:7}."
else
    echo "autodeploy: КРИТИЧНО — не поднялся и после отката"
    notify "🔴 КРИТИЧНО: бот не поднялся ни на новой, ни на старой версии. Нужна ручная переустановка."
fi
exit 1
