# Geliştirme Workflow'u

Bu dosya ajan workflow'unun tek kaynağıdır. Claude Code'daki `/devam` skill'i ve
opencode'daki `lead` (`/continue`) aynı döngüyü buradan uygular. Genel
çalışma kuralları `AGENTS.md`'de, güncel durum kökteki `STATE.md`'dedir.

- Birincil: Claude Code'da `/devam` veya `/devam 3`. Worker'lar arka planda
  `opencode run --agent <ajan>` ile etkileşimsiz çağrılır; bu ajanlarda `ask` ve
  `question` yoktur, karar gerekirse raporlarına yazarlar.
- Canlı izleme: `scripts/izle.sh` (tarayıcı için `scripts/izle.sh web`). Script
  `opencode serve --port 4096` açar ve izleme ekranını bağlar; ekran kapanınca
  kendi açtığı sunucuyu kapatır. `/devam` sunucuyu görürse worker'ları onun
  üzerinden çalıştırır; ekran en son `/devam` oturumunu kendisi açar ve yeni iş
  başlayınca ona geçer (oturum başlıkları `devam:` veya `gates` ile başlamalı).
  Sunucu kapalıysa da çalışır; oturumlar sonradan `opencode` → oturum listesinde görülür.
- Tercih edilen (kullanıcı değişiklikleri opencode'da izler): `opencode` TUI'sini aç (varsayılan ajan `lead`) ve `/continue` yaz.
  Lead etkileşimlidir: soru sorabilir, commit/push için onay ister.

## Roller ve modeller

| Rol | Nerede / model | Görev |
| --- | --- | --- |
| Orkestratör (birincil) | Claude Code, `/devam` | İş seçer, iş tanımı yazar, devreder, review eder, STATE günceller |
| Orkestratör (opencode) | opencode `lead`, `openai/gpt-6.1-sol`, `/continue` | Aynı döngü; önemli işlerde `scripts/claude-review.sh` ile Claude review |
| `implementer` | opencode, `openai/gpt-6.1-sol` | Muhakeme veya risk gerektiren uygulama |
| `fast-worker` | opencode, `openai/gpt-6-luna` | Mekanik ve düşük riskli uygulama |
| `checker` | opencode, `openai/gpt-6-luna` | Full quality gate'leri çalıştırır, dosya değiştirmez |
| `reviewer` | opencode, `openai/gpt-5.6-sol` | Salt-okunur review (küçük işler, Claude yokken veya Claude'un yazdığı işlerde) |

Review her zaman mümkün olduğunca **çapraz aile**dir: GPT'nin yazdığını Claude,
Claude'un yazdığını GPT inceler. Claude kullanılamadığında `reviewer`
kullanılır ve bu durum STATE'e "aynı aile review" olarak not edilir.

## Döngü (bir iş)

1. **Durum.** `STATE.md` ve `git status` okunur. STATE'te bilinmeyen,
   commit edilmemiş değişiklik varsa dur ve kullanıcıya sor.
2. **İş seçimi.** Aktif milestone'da açık must-fix varsa sıradaki iş odur;
   yoksa STATE'teki "Sıradaki iş" veya milestone içindeki en küçük anlamlı adım.
   Belgelenmemiş/çelişkili ürün davranışı gerekiyorsa **dur**: kullanıcıya
   sade Türkçe, seçenekleri ve en basit öneriyi içeren tek bir soru sor.
3. **İş tanımı.** Worker'a şu bölümlerle verilir:
   - Amaç (1-3 cümle)
   - Okunacaklar (dosya yolları, ilgili spec bölümleri)
   - Değiştirilebilecek dosyalar (liste; dışına çıkılmaz)
   - Kabul kriterleri ve yapılmayacaklar
   - Çalıştırılacak hedefli kontroller (exact komut + dizin)
4. **Yönlendirme.** Varsayılan `implementer`. `fast-worker` yalnız hepsi
   doğruysa: kabul kriterleri net, mevcut bir deseni birebir izliyor, yeni
   ürün/mimari kararı yok, kolay geri alınır, targeted testlerle doğrulanır ve
   aşağıdaki risk tetikleyicilerinden hiçbiri yok:
   - migration veya kalıcı veri modeli,
   - transaction / concurrency / ownership,
   - filesystem mutation, dosya silme, veri kaybı,
   - güvenlik veya path-containment,
   - API/data contract değişikliği.
   Boyut, dosya sayısı ve "hızlı biter" beklentisi ölçüt değildir. fast-worker
   işin içinde bir tetikleyici bulursa durur ve geri bildirir; iş implementer'a geçer.
5. **Review.** Orkestratör diff'i ve test sonucunu inceler. Her bulgu bir sınıf alır:
   - **must-fix** — davranış, veri bütünlüğü, güvenlik, spec ihlali veya önemli test eksikliği
   - **should-fix** — gerçek ama bloklamayan; STATE'e not, ayrı iş olabilir
   - **accepted-risk** — düşük olasılık/etki (bkz. AGENTS.md Proportionality); STATE'e tek satır
   - **over-engineering** — gereksiz karmaşıklık; sadeleştirme önerilir
   Reviewer yeni "kilitli karar" veya yeni ürün kuralı üretmez; böyle bir
   ihtiyaç kullanıcıya soru olarak gider.
6. **Düzeltme.** must-fix varsa aynı worker oturumuna **en fazla bir** düzeltme
   turu verilir. Sonra hâlâ must-fix kalıyorsa dur ve kullanıcıya raporla.
   Aynı yaklaşımı kör tekrarlama; orkestratör önce kök nedeni teşhis eder.
7. **Doğrulama.** Orkestratör hedefli kontrolleri kendisi yeniden çalıştırır
   (worker'ın "PASS" demesi yeterli değildir).
8. **Kayıt.** `STATE.md` güncellenir, sonra iş commit edilir: yalnız bu işin
   dosyaları + `STATE.md` tek tek stage edilir, tek commit atılır. Kullanıcıya kısa
   Türkçe özet verilir — kod değil **davranış** üzerinden (kullanıcı kodu okumaz):
   uygulamada ne değişti, kontroller, kalan risk, önerilen sıradaki iş.
   Push yapılmaz; yalnız kullanıcı "push et" dediğinde.

## Quality gate'ler

- Her işte yalnız ilgili hedefli kontroller.
- Milestone kapanışında (açık must-fix yokken) `checker` full gate'leri çalıştırır:
  - `backend/`: `.venv/bin/ruff check .`, `.venv/bin/mypy app tests`, `.venv/bin/pytest`
  - `frontend/`: `npm run lint`, `npm run typecheck`, `npm test`, `npm run build`, `npm run check:contrast`
- Gate sonucu STATE'e **tarih + makine** (macOS / Fedora) ile yazılır. Kaynak kod
  değişince önceki full gate sonucu bayat sayılır.

## STATE.md kuralları

- Üstüne yazılır; log/journal değildir. En fazla ~60 satır.
- Session ID, ham komut çıktısı, review turu kronolojisi yazılmaz. Geçmiş git'te durur.
- Bölümler: Şu an · Sıradaki iş · Açık kararlar · Açık bulgular · Son gate · Bilinen riskler · Sonraki fazlar.

## Durma koşulları

Şunlardan biri olursa iş açık bırakılır, STATE'e kısa neden yazılır ve durulur:

- kullanıcı kararı gereken ürün sorusu,
- düzeltme turundan sonra çözülmeyen must-fix,
- provider / kota / kimlik doğrulama hatası (implementation hatası sayılmaz;
  aynı çağrı tekrar tekrar denenmez),
- izin reddi veya etkileşimli onayda takılma (izin aşmak için yol aranmaz),
- beklenmeyen git değişikliği,
- iş tanımının dışına çıkma ihtiyacı.

## Git

- Commit: review ve kontrollerden geçen her iş orkestratör tarafından otomatik
  commit edilir (döngü adım 8). Worker'lar commit etmez. Review'dan geçmeyen
  veya durma koşuluna takılan iş commit edilmez; STATE'te açık kalır.
- Push: yalnız kullanıcı "push et" dediğinde.
- Force push, amend, rebase, `reset --hard` ve geçmişi değiştiren her işlem: asla
  (kullanıcı açıkça istemedikçe).
Makineler arası geçiş: commit + push, diğer makinede pull. Her makinede
`backend/.venv` ve `frontend/node_modules` ayrı kurulur (git'e girmez).
