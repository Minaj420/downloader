#!/usr/bin/env bash
# /devam'ın opencode'da yaptığı işi canlı izle.
#
# Kullanım:
#   scripts/izle.sh        terminalde izle (en son /devam işini açar, yenisi başlayınca ona geçer)
#   scripts/izle.sh web    tarayıcıda izle (oturumu listeden sen seçersin)
#
# Sunucu zaten açıksa ona bağlanır; değilse açar ve script bitince kapatır.
# Çıkmak için izleme ekranından normal şekilde çık (ctrl+c).

set -euo pipefail

PORT=4096
URL="http://127.0.0.1:${PORT}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${1:-tui}"
cd "$REPO"

server_up() {
  curl -s -o /dev/null "$URL"
}

# En yeni /devam oturumunun ID'si (başlığı "devam:" veya "gates" ile başlayan); yoksa boş.
latest_session() {
  curl -s "$URL/session" | python3 -I -c '
import json, sys
try:
    sessions = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
ours = [s for s in sessions
        if s.get("directory") == sys.argv[1]
        and str(s.get("title", "")).startswith(("devam:", "gates"))]
if ours:
    print(max(ours, key=lambda s: s["time"]["created"])["id"])
' "$REPO"
}

if [[ "$MODE" == "web" ]]; then
  if server_up; then
    echo "Sunucu zaten açık: tarayıcıda $URL adresini aç."
    exit 0
  fi
  exec opencode web --port "$PORT"
fi

server_pid=""
switch_flag="$(mktemp)"
cleanup() {
  rm -f "$switch_flag"
  [[ -n "$server_pid" ]] && kill "$server_pid" 2>/dev/null || true
}
trap cleanup EXIT

if ! server_up; then
  echo "opencode sunucusu başlatılıyor..."
  opencode serve --port "$PORT" >/dev/null 2>&1 &
  server_pid=$!
  for _ in $(seq 1 40); do
    server_up && break
    sleep 0.5
  done
  if ! server_up; then
    echo "Sunucu başlatılamadı." >&2
    exit 1
  fi
fi

current="$(latest_session)"
while [[ -z "$current" ]]; do
  echo "Henüz /devam işi yok; Claude Code'da /devam yazınca burada açılacak..."
  sleep 3
  current="$(latest_session)"
done

while true; do
  : > "$switch_flag"
  # Arka planda yeni /devam oturumu bekle; gelirse izleme ekranını kapatıp ona geç.
  (
    while sleep 3; do
      newest="$(latest_session)"
      if [[ -n "$newest" && "$newest" != "$current" ]]; then
        echo "$newest" > "$switch_flag"
        pkill -INT -f "opencode attach $URL --dir $REPO --session $current" || true
        break
      fi
    done
  ) &
  watcher_pid=$!

  opencode attach "$URL" --dir "$REPO" --session "$current" || true

  kill "$watcher_pid" 2>/dev/null || true
  wait "$watcher_pid" 2>/dev/null || true
  stty sane 2>/dev/null || true

  next="$(cat "$switch_flag")"
  if [[ -z "$next" ]]; then
    break  # kullanıcı ekrandan kendisi çıktı
  fi
  current="$next"
  clear
  echo "Yeni /devam işi başladı, ona geçiliyor..."
done
