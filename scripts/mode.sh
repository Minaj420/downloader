#!/usr/bin/env bash
# Claude Code izin modunu değiştirir: .claude/modes/<mod>.settings.json -> .claude/settings.json.
# Kullanım: bash scripts/mode.sh claude-only|normal|status
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
modes="$root/.claude/modes"
target="$root/.claude/settings.json"

active() {
  if cmp -s "$target" "$modes/normal.settings.json"; then
    echo normal
  elif cmp -s "$target" "$modes/claude-only.settings.json"; then
    echo claude-only
  else
    echo unknown
  fi
}

case "${1:-status}" in
  status)
    echo "Aktif mod: $(active)"
    ;;
  claude-only|normal)
    if [ "$(active)" = unknown ]; then
      echo "HATA: .claude/settings.json hiçbir mod dosyasıyla eşleşmiyor; üzerine yazılmadı." >&2
      exit 1
    fi
    tmp="$(mktemp "$target.XXXXXX")"
    cp "$modes/$1.settings.json" "$tmp"
    mv "$tmp" "$target"
    echo "Aktif mod: $(active). İzinler yenilenmediyse Claude Code oturumunu yeniden başlat."
    ;;
  *)
    echo "Kullanım: bash scripts/mode.sh claude-only|normal|status" >&2
    exit 2
    ;;
esac
