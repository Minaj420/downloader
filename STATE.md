# Durum

Devam özeti; iş akışı `docs/WORKFLOW.md`.
## Şu an
- Mod: Normal, lead OpenCode; bekleyen Claude-only GPT review'u yok.
- Lead/reviewer/implementer tanımı GPT‑6.2 Sol (implementer pasif); Luna/Claude değişmedi. ARCH-A tamam: smoke/kapanış STATE'te; ARCH bayat durumları güncellenir, karar çatışmasında durulur.
- M6 açık; Q0/18 ve runtime6 kullanıcı Compose smoke/onayıyla kapandı. M6-Q genel kapanışı yok.
- Uzlaştırma yedek dalı korunur; push yalnız açık kullanıcı talebi ve hedefle.
- Onaylı sabit profillerin yetkili belgeye aktarımı bekliyor: MP3 V0, M4A AAC 256k,
  Opus 160k, WAV/FLAC 16-bit; MP4 x264 CRF20 medium + AAC 192k. Yeni ürün kararı değil.
- Onaylı, belgeye aktarılacak: videodan ses çıkarma yok; ses doğrudan ses akışından.
## Sıradaki iş
- Onaylı v0.1 sırası ve v0.2 ertelemeleri: ARCHITECTURE §59 ve PRODUCT_SPEC.
- Bulgu 3a kapandı: Fedora Compose ffprobe 7.1.5; tekil 720p, dikey 1080p; ağ hatalı öğe Retry ile indi (4/4), sıra korundu. Hedefli 817 PASS/1 SKIP, Ruff/mypy ve review temiz; göreli girdi testi should-fix.
- Q3b-2 ve 3c Already exists v0.1 sonrası; güvenli numara ve çözünürlük eki kalır. Aday ve açık review bulguları `wip/already-exists` dalında.
- M6-Q20 kapandı: Compose seçici/Ses PASS, Shorts 4/4, 1080×1920 doğrulandı; tavan dışı yedek otomatik PASS, uygun canlı kaynak yok, kullanıcı kapanışı onayladı. Sonraki Q3 Clear History; katalog eşleşmesi (§16) gerekirse önce koleksiyonlarda.
- Bekleyen toplu UI Compose smoke: Queue'da active/history; gezinme/F5 sonrası sunucudan iş erişimi.
- Smoke: uygun Cancel/Retry ve çift tık/pending.
- Smoke: geçmiş iş detay/Activity, collection öğeleri; Load more ve yeniden bağlantıda güncellik.
- Smoke: Private video içeren playlist; satır devre dışı, Select All ve sayaç dışında.
- 2026-11-22 kullanıcı Compose Q2: Retry kaldığı yerden devam etti ve indirme tamamlandı.
- M6.4/5 kapanışı yok; full gate ve değişen davranış için yeni kullanıcı Compose smoke'u gerekir.
## Açık kararlar
- M6.6 güvenilir albüm grubu/ön hazırlık ve çoklu album_artists klasör değeri; M6.7 cover.jpg seçimi açık.
- M6.9 yalnız seçim ekranından kullanıcı isterse; MB/CAA aday, hak/API/embed koşulları o işte netleşecek.
- Mutagen lisansı kişisel kullanıma engel değil; dağıtım uyumu notu sürer. M9 kalite/boyut bekler.
- S1 M7 hedef doğrulamasıyla onaylı; açılış yaz-sil probu/kalıcı hata bandı planlı; S2 v0.1 sonrası.
- v0.1 platform ikonları M6.5 UI'da; ikon seti ve lisans açık.
- Onaylı Retry/retention, failed staging ve K3 yetkili belgelerde; K2 ve tema v0.2.
## Açık bulgular
- should-fix: eski conversion durumları PRODUCT_SPEC §8–9 ve ARCHITECTURE M6
  kapanış paragrafında bayat; sabit profillerin yetkili belgeye aktarımı ayrı iş.
- should-fix: collection başarılı convert/caller-cancel testi sleep(0) ile deterministik değil;
  single başarılı convert+iptal ek testi eksik. Üretim hatası bulunmadı.
- Audit (ARCH §59): Settings → S1/M7; son seçim localStorage v0.1. Library detay (M9-L): başlığa tıklanabilirlik belirsiz, detay sayfa sonunda; popup/satır içi genişleme kararı açık.
- New Download adı v0.1'de kalır; History satır içi detay M6.5 UI'a alındı. K2 v0.2.
- M6.5 UI onaylı: tekil videoda kaynakla desteklenen standart kısa kenar tavanları, varsayılan `Best Available`, ayrı çıktı biçimi. Küçük `Up to`; seçilen satır bilinen kaynak boyutunu açar, ayrı Exact modu yok. Stil ve PRODUCT_SPEC §9 uyumu bekliyor. Media Preview “Single item” etiketi sadeleştirilecek; diğer UI notları ARCH §59.
- network_error/Deno bağı kanıtsız. Analiz +25/hepsi v0.2.
- Fedora Python 3.14: child storage pidfd testi düzeltme diff'inde geçti; full gate çalışmadı.
- should-fix: child Skip Existing Activity uyarısı yok; ayrı lifecycle/transaction işi.
- should-fix: modelsiz implement resume CLI hatasının belgelenmesi ve izleyici partial-line
  testinin deterministikliği açık; restart/model reload ve farklı-rol resume izolasyonu kanıtsız.
- should-fix: lead.md eski bootstrap referansı.
- Takip: scripts/claude-agent.py review prompt'unu yeni review sınıf sınırıyla eşitle; ARCH §59 zamanlaması erteleme kararı öncesinden.
- should-fix: geniş git diff/log izinleri --output seçeneğini teknik olarak dışlamıyor;
  yalnız salt-okunur biçimler kullanılır, config bakımı ayrı. gh PR wildcard'ları da repo/hedef/
  body-file sınırını sağlamaz; açık repo/base/head ve düz metin body kullanılmalı.
- Canlı slash yüklenmesi ve tüm ask/deny/yol dışı izin sınırları doğrulanmış değil.
- Teknik takip: non-root UID/GID→M7/S1 ayrı Opus işi; yt-dlp sabitleme, Python3.12 CI.
## Son gate
- 2026-11-20 macOS Q1-fe-page: lead149 test/typecheck/lint ve kontrast20 PASS; GPT review temiz; Docker/full gate yok.
- 2026-11-19 kullanıcı Fedora Compose runtime6 PASS: Deno2.9.8/yt-dlp2026.10.26/EJS0.8.0 algılandı; UI video PASS.
- Network script/Q19 son tek-satır/tekrarsız/detail≤500 PASS; eski audio restart BLOCKED sonucu aşıldı.
- 2026-11-20 kullanıcı Compose video/Original restart PASS20s: failed, Exit0/OOMyok, shutdown, retryable, recovery.
- 2026-11-17 kullanıcı Fedora Compose Q18 PASS: single/collection5.5s, idle0.8s, Exit0/OOMfalse, exec doğrulandı.
- Mix4 sabit seçim/completed korundu, auto-resume yok, Retry46→48; MKV VP9/Opus200s tam decode kullanıcı PASS.
- Host RPM Fusion FFmpeg smoke 23 PASS/0 SKIP; container libx264 ayrıca görüldü. Sonraki skip'i bildir.
- 2026-10-30 macOS full gate tarihsel; sonraki uzlaştırma kaynak değişikliğiyle bayat.
## Bilinen riskler
- Accepted: YouTube'da aynı gerçek başlık ve eksik availability yanlışlıkla elenebilir.
- Accepted: smoke idle kontrolü atomik değil; testte başka iş başlatılmaz; kullanıcı ağ/restart kurtarma PASS.
- Accepted: Original remux EXIT_STATUS içindeki disk/izin hatası genel uyarıda kalabilir (PRODUCT_SPEC §14).
- Accepted: commit anında iptal canlı child uyarısını kaçırabilir; commit edilmiş
  uyarı DB'de kalır; Activity sorgusu veya uygun cursorla replay'den erişilir.
- Accepted: private tek yazıcılı staging lstat/FFmpeg açılışı mikro yarışı sürer;
  eski token'lı çıktılar cleanup'a kadar disk kullanır. Mutlak race koruması değil.
- Accepted: kill sonrası 5 sn reap + en fazla 1 sn waiter drain; D-state garantisi yok,
  ek iptaller özgün kesintiyi değiştirmez; process-group kapsam dışı.
- Accepted: eski snapshot AAC/profil bilgisi düzeltilmedi; MPEG 69/6b/40.34 layer belirsiz;
  eksik/bozuk/diğer H.264 ve interlaced copy her cihazda uyumlu olmayabilir.
  Kaynak etiketi doğrulanmaz; timeout 3600 sn. Bozuk profil DTO/SSE'yi bozabilir; sessiz Original fallback yok.
- Gerçek ağ/container kurtarma denendi; collection remux ve dönüşüm iptali kullanıcı smoke'unda denenmedi.
- M6.2 containment planlama anında; nested yazmadan önce writer kök/parent doğrulamalı;
  root içi symlink davranışı TOCTOU koruması değil. Container root, yt-dlp pinsiz; hard link gerekli.
- Claude izinleri OS sandbox değil; built-in plugin'ler kalır, log kaynak metni içerebilir;
  izleyiciyi kapatmak işi durdurmaz, ana çağrı açık kalmalı.
- Accepted: Delete File stat/unlink penceresi, bazı terminal parent retryable alanları,
  ertelenmiş cancel 404/event temizliği; Elisa M4A sorunu oynatıcıya özgü.
- Accepted: HTTP5s istekleri iptal edebilir; Compose30s aşımı ve migration sırasında stop için crash recovery sınırı sürer.
- Starlette httpx→httpx2 deprecation uyarısı: şimdi iş değil, bağımlılık bakımında ele alınacak.
