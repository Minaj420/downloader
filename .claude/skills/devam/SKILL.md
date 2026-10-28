---
name: devam
description: Projenin sıradaki işini (veya N işi) otomatik yürüt — Claude planlar ve review eder, opencode'daki GPT uygular. Kullanıcı "/devam", "devam et", "sıradaki işi yap" dediğinde kullan.
argument-hint: "[iş sayısı (varsayılan 1, en fazla 5) veya kısa talimat]"
---

# /devam

`docs/WORKFLOW.md` döngüsünü birincil orkestratör olarak uygula. Kurallar oradadır;
bu dosya yalnız Claude Code tarafındaki somut adımları verir. Kullanıcıyla Türkçe konuş.

Argüman: `$ARGUMENTS` — sayıysa en fazla o kadar iş (üst sınır 5), boşsa 1;
metinse kapsamı daraltan talimat.

## Her iş için

1. **Durum.** `STATE.md`'yi oku, `git status --short` çalıştır. STATE'te açıklanmayan
   commit edilmemiş değişiklik varsa dur ve sor.

2. **İş tanımı.** `docs/WORKFLOW.md` §Döngü 2–3'e göre işi seç. Ürün kararı gerekiyorsa
   AskUserQuestion ile sade Türkçe sor (en basit seçenek önerilen) ve dur.
   İş tanımını `tmp/devam/<kisa-ad>.md` dosyasına yaz (`tmp/` git'e girmez).
   Kontrol komutlarını worker için `backend/` veya `frontend/` workdir'iyle yaz.

3. **Devret.** WORKFLOW §4'e göre `implementer` veya `fast-worker` seç.
   Kullanıcı canlı izlemek için `opencode serve --port 4096` açmış olabilir:
   `curl -s -o /dev/null http://127.0.0.1:4096` başarılıysa aşağıdaki **tüm** `opencode run`
   çağrılarına `--attach http://127.0.0.1:4096 --dir <repo-kökü-mutlak-yol>` ekle
   (kullanıcı `opencode attach http://127.0.0.1:4096` ile oturumu canlı görür); değilse ekleme.
   Repo kökünden, `run_in_background: true` ile çalıştır ve tamamlanma bildirimini bekle:

   ```bash
   opencode run --agent <implementer|fast-worker> --format json --title "devam: <kisa-ad>" \
     -f tmp/devam/<kisa-ad>.md "Ekteki iş tanımını uygula ve raporla." > tmp/devam/<kisa-ad>.jsonl 2>&1
   ```

   Sonra özetle: `python3 -I .claude/skills/devam/summarize.py tmp/devam/<kisa-ad>.jsonl`
   (session ID'yi not et). `STATUS: INCOMPLETE` (non-zero çıkış) ise review'a geçme:
   provider/kota hatasıysa tekrar deneme ve dur; worker "iş tanımı belirsiz / kapsam dışı"
   dediyse bunu kullanıcı kararı veya yeni iş tanımı olarak ele al.

4. **Review (çapraz aile).** `git diff` ile değişikliği incele — worker'ın raporuna değil
   koda bak. Bulguları must-fix / should-fix / accepted-risk / over-engineering olarak sınıfla
   (AGENTS.md Proportionality). Yeni ürün kuralı icat etme.

5. **Düzeltme (en fazla bir tur).** must-fix varsa düzeltme talimatını
   `tmp/devam/<kisa-ad>-fix.md`'ye yaz ve aynı oturuma gönder:

   ```bash
   opencode run --session <session-id> --agent <aynı-ajan> --format json \
     -f tmp/devam/<kisa-ad>-fix.md "Ekteki review bulgularını düzelt ve raporla." > tmp/devam/<kisa-ad>-fix.jsonl 2>&1
   ```

   Tekrar review et. Hâlâ must-fix varsa dur ve kullanıcıya raporla.

6. **Doğrula.** Hedefli kontrolleri kendin, repo kökünden sessiz modda çalıştır:
   - `backend/.venv/bin/pytest -q -c backend/pyproject.toml backend/tests/<dosya>.py`
   - `backend/.venv/bin/ruff check --config backend/pyproject.toml <yollar>`
   - `backend/.venv/bin/mypy --config-file backend/pyproject.toml <yollar>`
   - `npm --prefix frontend run <lint|typecheck|build>`, `npm --prefix frontend test -- <test-dosyası>`

7. **Kayıt.** `STATE.md`'yi WORKFLOW kurallarına göre güncelle (üstüne yaz, ≤60 satır).
   Sonra commit et: işin dosyalarını ve `STATE.md`'yi `git add -- <dosya>` ile tek tek
   ekle (`.`/`-A` yok), `git diff --cached --stat` ile kontrol et, tek commit at.
   Mesaj davranışı anlatır; sona sistemin verdiği Co-Authored-By satırını ekle.
   Push yapma.

## Milestone kapanışı

Açık must-fix kalmadığında ve milestone'un işleri bittiğinde full gate'leri ucuz
checker'a çalıştır (uzun çıktı Claude limitini yemesin):

```bash
opencode run --agent checker --format json --title "gates" "Full gate'leri çalıştır ve raporla." > tmp/devam/gates.jsonl 2>&1
```

Sonucu tarih + makine (macOS/Fedora) ile STATE'e yaz.

## Bitiş

Kullanıcıya kısa Türkçe özet — kullanıcı kodu okumaz, uygulamanın **davranışı** üzerinden
anlat: uygulamada ne değişti, commit(ler), kontroller, review sonucu, kalan risk, önerilen
sıradaki iş. Push yalnız kullanıcı "push et" derse.
