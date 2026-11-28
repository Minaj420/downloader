# Durum

Devam özeti; iş akışı `docs/WORKFLOW.md`.
## Şu an
- Mod: Normal, lead OpenCode; bekleyen Claude-only GPT review'u yok.
- Lead/reviewer/implementer tanımı GPT‑6.2 Sol (implementer pasif); Luna/Claude değişmedi. ARCH-A tamam: smoke/kapanış STATE'te; ARCH bayat durumları güncellenir, karar çatışmasında durulur.
- M6 kapandı: kullanıcı Compose smoke ve macOS tam gate; M6 kapsamında plansız v0.1 zorunluluğu yok.
- M6.5 kapandı: metin etiketi, sunum UI ve temel hata metinleri; bu kapsamda plansız v0.1 zorunluluğu yok.
- Uzlaştırma yedek dalı korunur; push yalnız açık kullanıcı talebi ve hedefle.
## Sıradaki iş
- Bulgu 3a kapandı: Fedora Compose ffprobe 7.1.5; tekil 720p, dikey 1080p; ağ hatalı öğe Retry ile indi (4/4), sıra korundu. Hedefli 817 PASS/1 SKIP, Ruff/mypy ve review temiz; göreli girdi testi should-fix.
- Q3b-2 ve 3c Already exists v0.1 sonrası; güvenli numara ve çözünürlük eki kalır. Aday ve açık review bulguları `wip/already-exists` dalında.
- M6-Q20 kapandı: Compose seçici/Ses PASS, Shorts 4/4, 1080×1920 doğrulandı; tavan dışı yedek otomatik PASS, uygun canlı kaynak yok, kullanıcı kapanışı onayladı. Katalog eşleşmesi (§16) gerekirse önce koleksiyonlarda.
- Q3 `main`'de; deneysel dal M7'yi engellemiyor. Compose Q3 UI PASS: 18/20 önizleme, varsayılan ve iptaller dahil silme, restart sonrası eski önizleme reddi, Library katalog kaydı; düğme geometrisi ve oynatma denenmedi.
- M7-StartupProbe kapandı: 28 Kasım macOS Compose kullanıcı smoke PASS; normal açılış, salt okunur Plex'te uyarı ve sağlık 200, yazılabilir Plex'te uyarı ve kalıntı yok. Sırada S1 API/Ayarlar ve uyarı bandı.
- M7 sonrası v0.1: M6.7 yeni ses parçalarına gömülü kaynak görseli (ARCH §59); henüz başlanmadı.
- M6.5-T2/T3 Fedora Compose PASS (Mutagen 1.48.1, MP3 etiket ve WAV); T3 hedefli 791 test/Ruff/mypy ve review PASS.
- M6.5 metin etiketi Compose PASS: Opus/MP3 etiket 2/2, MP3 oynatma, WAV 2/2 uyarısız, Shorts 2/2 oynatma/sıra, ses iptal 1/2 → Retry 2/2 kopyasız. ARCH §14 probe başlığı notu should-fix.
- Accepted (M6.5-T2): düşen/yinelenen TXXX değişmeden reddedilebilir (ID3v2.4 dahil). Should-fix: seek hata ayrıntısı ve ses-önce çoklanmış Ogg testi.
- Toplu UI Compose smoke (28 Kasım) kullanıcı PASS 4/4: gezinme/F5 ve Load more; Details/History Retry ve Cancel; Private öğe seçilemiyor, Select All ve sayaç dışında; ağ hatalı playlist öğesinde Queue detayı ve New Download'da yalnız anlaşılır hata mesajı.
- M6-LastSelection kullanıcı Compose PASS 3/3: F5 sonrası URL/taslak boş, yeniden analizde 720p ve MP4 geri geldi, ses kaynağında varsayılan kullanıldı. Should-fix: aynı sayfada tekrarlanan analiz kimliğiyle tekil MP3 geçiş testi.
- Kullanıcı Compose PASS: Q2 Retry kaldığı yerden tamamlandı; Q3-ACT Activity/Library ve F5 sonrası geçmiş korundu. M6.5-UI-AutoPlatform (28 Kasım) PASS: YouTube → SoundCloud → YouTube etiketleri doğru; SoundCloud parçası indirildi.
## Açık kararlar
- M6.6 güvenilir albüm grubu/ön hazırlık ve çoklu album_artists klasör değeri; M6.7 cover.jpg seçimi açık.
- M6.9 yalnız seçim ekranından kullanıcı isterse; MB/CAA aday, hak/API/embed koşulları o işte netleşecek.
- Mutagen lisansı kişisel kullanıma engel değil; dağıtım uyumu notu sürer. M9 kalite/boyut bekler.
- S1 M7 hedef doğrulamasıyla onaylı; açılış yaz-sil probu/kalıcı hata bandı planlı; S2 v0.1 sonrası.
- M6.5 platform ikonları: YouTube Music/Shorts görsel PASS, Other yalnız otomatik test. Lisans/marka doğrulaması ertelendi.
## Açık bulgular
- should-fix: eski conversion durumları PRODUCT_SPEC §8–9 ve ARCHITECTURE M6
  kapanış paragrafında bayat.
- should-fix: collection başarılı convert/caller-cancel testi sleep(0) ile deterministik değil;
  single başarılı convert+iptal ek testi eksik. Üretim hatası bulunmadı.
- Q3 should-fix: hata zincirinde yol gizleme ve fstat hatasında fd kapanış testi. M7/S1 bekliyor; M9-L tıklanabilirlik ve popup/satır içi genişleme açık.
- New Download adı v0.1'de kalır; K2 v0.2. M6.5-UI-History should-fix: yeniden bağlantıda taşınan panel içindeki klavye odağı kaybolabilir.
- Kullanıcı Compose smoke PASS (28 Kasım): Preview/kalite 1080×1920; çoklu History/Activity, Retry ve üst panel kapatma. Test takibi: yalnız canlı Activity olayı ve Retry'a uygun toplu fixture.
- network_error/Deno bağı kanıtsız. Analiz +25/hepsi v0.2. Should-fix M6.5-UI-Errors: Queue detayında çocuk hata metni için doğrudan test.
- Fedora Python 3.14: child storage pidfd testi düzeltme diff'inde geçti; full gate çalışmadı.
- should-fix: child Skip Existing Activity uyarısı yok; ayrı lifecycle/transaction işi. Queue satırsız kayıp Retry yanıtı + polling testi açık.
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
- 2026-11-28 macOS Docker Desktop/Linux VM Q3 smoke: Storage 3/3 PASS (harf duyarlı test bind'leri), API 3/3 PASS (named volume, host bind yok; kullanıcı sonucu). Homelab doğrulanmadı; fail-closed koruma desteklenen kurulumda yanlış ret verebilir.
- 2026-11-19 kullanıcı Fedora Compose runtime6 PASS: Deno2.9.8/yt-dlp2026.10.26/EJS0.8.0 algılandı; UI video PASS.
- Network script/Q19 son tek-satır/tekrarsız/detail≤500 PASS; eski audio restart BLOCKED sonucu aşıldı.
- 2026-11-20 kullanıcı Compose video/Original restart PASS20s: failed, Exit0/OOMyok, shutdown, retryable, recovery.
- 2026-11-17 kullanıcı Fedora Compose Q18 PASS: single/collection5.5s, idle0.8s, Exit0/OOMfalse, exec doğrulandı.
- Mix4 sabit seçim/completed korundu, auto-resume yok, Retry46→48; MKV VP9/Opus200s tam decode kullanıcı PASS.
- Host RPM Fusion FFmpeg smoke 23 PASS/0 SKIP; container libx264 ayrıca görüldü. Sonraki skip'i bildir.
- 2026-11-28 macOS M6 tam gate PASS: backend Ruff/mypy, pytest 3335 geçti/30 atlandı/4 uyarı (1 Linux mountinfo, 29 eksik Vorbis/Theora); frontend lint/typecheck/370 test/build/kontrast 20 PASS. macOS Bash 3.2 smoke betiğiyle uyumsuz.
## Bilinen riskler
- Accepted: YouTube'da aynı gerçek başlık ve eksik availability yanlışlıkla elenebilir. Queue Retry hatası yüzey kaybolunca tekrar seslendirilebilir.
- Accepted: smoke idle kontrolü atomik değil; testte başka iş başlatılmaz; kullanıcı ağ/restart kurtarma PASS.
- Accepted: Original remux EXIT_STATUS içindeki disk/izin hatası genel uyarıda kalabilir (PRODUCT_SPEC §14).
- Accepted: commit anında iptal canlı child uyarısını kaçırabilir; commit edilmiş
  uyarı DB'de kalır; Activity sorgusu veya uygun cursorla replay'den erişilir.
- Accepted: private staging lstat/FFmpeg açılışı, stat/unlink ve inode'a yerinde yazma sınırı sürer;
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
- Accepted: HTTP5s iptali, Compose30s ve migration stop kurtarma sınırı. Q3 §59: sistemik SQLite COMMIT hatasında taze okuma yok, 500 dönülür ve sonraki işler korunur; kayıt kaldıysa Retry baştan başlayabilir. DB düzelince yeni Clear önizlemesi; kod işi yeniden açılmaz.
- Accepted (M7-StartupProbe): probe stat→unlink penceresinde yabancı dosya silinebilir; attacker already has write access; random probe name, deleted immediately.
- Starlette httpx→httpx2 deprecation uyarısı: şimdi iş değil, bağımlılık bakımında ele alınacak.
