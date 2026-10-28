#!/usr/bin/env bash
# /devam'ın opencode'da yaptığı işi canlı izle.
#
# Kullanım:
#   scripts/izle.sh        terminalde izle
#   scripts/izle.sh web    tarayıcıda izle
#
# Sunucu zaten açıksa ona bağlanır; değilse açar ve izleme ekranı
# kapanınca kendi açtığı sunucuyu da kapatır.

set -euo pipefail

PORT=4096
URL="http://127.0.0.1:${PORT}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${1:-tui}"
cd "$REPO"

server_up() {
  curl -s -o /dev/null "$URL"
}

if [[ "$MODE" == "web" ]]; then
  if server_up; then
    echo "Sunucu zaten açık: tarayıcıda $URL adresini aç."
    exit 0
  fi
  exec opencode web --port "$PORT"
fi

started=0
if ! server_up; then
  echo "opencode sunucusu başlatılıyor..."
  opencode serve --port "$PORT" >/dev/null 2>&1 &
  server_pid=$!
  started=1
  for _ in $(seq 1 40); do
    server_up && break
    sleep 0.5
  done
  if ! server_up; then
    echo "Sunucu başlatılamadı." >&2
    kill "$server_pid" 2>/dev/null || true
    exit 1
  fi
fi

if [[ $started -eq 1 ]]; then
  trap 'kill "$server_pid" 2>/dev/null || true' EXIT
fi

echo "İzleme ekranı açılıyor. Oturum listesi: /sessions (veya ctrl+x l). GPT'ye mesaj yazma."
opencode attach "$URL" --dir "$REPO"
