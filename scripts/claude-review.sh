#!/usr/bin/env bash
# opencode lead'in Claude Code'u salt-okunur reviewer olarak çağırması için.
# Kullanım: scripts/claude-review.sh "<neyin review edileceği / odak>"
# Claude dosya değiştiremez; yalnız okur ve git okuma komutları çalıştırır.
set -euo pipefail
cd "$(dirname "$0")/.."

focus="${1:-Çalışma ağacındaki commit edilmemiş değişiklikleri incele.}"

# Araç listeleri tek argüman (virgülle); variadic bayrak komut metnini yutmasın diye
# komut metni stdin'den verilir.
claude -p \
  --permission-mode dontAsk \
  --allowedTools "Read,Grep,Glob,Bash(git status:*),Bash(git diff:*),Bash(git log:*),Bash(git show:*)" \
  --disallowedTools "Edit,Write,NotebookEdit" <<PROMPT
Salt-okunur review yap. Türkçe cevap ver. Önce AGENTS.md (Proportionality) ve
docs/WORKFLOW.md'yi oku. Dosya değiştirme. Odak: doğruluk, regresyon, spec uyumu,
güvenlik, veri bütünlüğü, eksik test, gereksiz karmaşıklık. Stil bulgu değildir.
Her bulgu: must-fix / should-fix / accepted-risk / over-engineering sınıfı, dosya:satır,
neden, somut düzeltme. Yeni ürün kuralı icat etme. Son satır: 'must-fix yok' veya
'must-fix var (N)'.

Review konusu: ${focus}
PROMPT
