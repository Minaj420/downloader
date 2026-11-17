# Durum

Devam özeti; iş akışı `docs/WORKFLOW.md`.

## Şu an

- Mod: Normal, lead OpenCode; bekleyen Claude-only GPT review'u yok.
- M6 açık; M6-Q Queue önkoşulu etiket uygulamasından önce. Bugün yalnız kayıt/plan; uygulama onaysız.
- Uzlaştırma yedek dalı korunur; push yalnız açık kullanıcı talebi ve hedefle.
- Onaylı sabit profillerin yetkili belgeye aktarımı bekliyor: MP3 V0, M4A AAC 256k,
  Opus 160k, WAV/FLAC 16-bit; MP4 x264 CRF20 medium + AAC 192k. Yeni ürün kararı değil.
- Onaylı, belgeye aktarılacak: videodan ses çıkarma yok; ses doğrudan ses akışından.

## Sıradaki iş

- Kullanıcı Queue planını onaylayacak; öneri: kapanış18 → teşhis19 → runtime6 → Queue erişim/control.
- Ardından yanlış çıktı/çakışma3 ve Queue toplu temizlik10; alt adımlar ARCHITECTURE §59 M6-Q'da.
- M6.5 Mutagen eşleme/adapter ve pipeline bekler; metin → M6.6 grup/yollar → M6.7 kapak sırası korunur.
- M6.4/5 kapanışı yok; full gate ve değişen davranış için yeni kullanıcı Compose smoke'u gerekir.

## Açık kararlar

- M6.6 güvenilir albüm grubu/ön hazırlık ve çoklu album_artists klasör değeri; M6.7 cover.jpg seçimi açık.
- M6.9 yalnız seçim ekranından kullanıcı isterse; MB/CAA aday, hak/API/embed koşulları o işte netleşecek.
- Mutagen lisansı kişisel kullanıma engel değil; dağıtım uyumu notu sürer. M9 kalite/boyut bekler.
- Queue: tekli cancelled Retry/retention, failed staging temizliği, K2 taslak kurtarma onay bekler.
- Koleksiyon çözünürlük seçicisi20 ve K3 kalıcı kapsam sorusu öneri; kullanıcı spec denetimi bekleniyor.

## Açık bulgular

- should-fix: eski conversion durumları PRODUCT_SPEC §8–9 ve ARCHITECTURE M6
  kapanış paragrafında bayat; sabit profillerin yetkili belgeye aktarımı ayrı iş.
- should-fix: collection başarılı convert/caller-cancel testi sleep(0) ile deterministik değil;
  single başarılı convert+iptal ek testi eksik. Üretim hatası bulunmadı.
- Öncelikli: K1 Queue placeholder; K2 New Download analiz/seçim/kart gezinme/F5'te kayboluyor.
- Öncelikli18: aktif işte stop-t60 iki kez force-kill; indirme sürüyor. SSE beklemesi güçlü hipotez.
- Öncelikli3/6/10/19: yanlış kalite skip, eksik JS runtime, staging birikimi, görünmeyen hata ayrıntısı.
  Kullanıcı gözlemleri ve diğer UX bulguları ARCHITECTURE §59'da; network_error/deno bağı kanıtsız.
- Fedora Python 3.14 eski üç backend failure (os.close monkeypatch/pidfd) macOS gate ile doğrulanmadı.
- should-fix: child Skip Existing Activity uyarısı yok; ayrı lifecycle/transaction işi.
- should-fix: modelsiz implement resume CLI hatasının belgelenmesi ve izleyici partial-line
  testinin deterministikliği açık; restart/model reload ve farklı-rol resume izolasyonu kanıtsız.
- should-fix: lead.md eski bootstrap referansı ayrı bakımda değerlendirilmeli.
- should-fix: geniş git diff/log izinleri --output seçeneğini teknik olarak dışlamıyor;
  yalnız salt-okunur biçimler kullanılır, config bakımı ayrı. gh PR wildcard'ları da repo/hedef/
  body-file sınırını sağlamaz; açık repo/base/head ve düz metin body kullanılmalı.
- Canlı slash yüklenmesi ve tüm ask/deny/yol dışı izin sınırları doğrulanmış değil.
- Teknik takip: container user, yt-dlp sabitleme/görünürlük, Python 3.12 CI ve ARCHITECTURE bölme;
  fazların yetkili sırası ARCHITECTURE §59'da, kapanış süresi öncelikli18 kapsamında.

## Son gate

- 2026-11-17 Fedora, M6.5c: prepared/collection/single/output-profile 857 PASS;
  hedefli Ruff/mypy PASS; full gate ve yeni gerçek FFmpeg smoke çalıştırılmadı.
- 2026-11-17 kullanıcı Fedora Compose raporu: Original/MP3, MP4 copy/x264, playlist, Cancel/Retry PASS.
- Güncellenen stop-t2 kurtarma PASS: completed korunur, active interrupted_by_restart, no autoresume;
  sayaç1/1/3, kart canlı, dosya644root host'ta oynar. Düzenli kapanış18 FAIL; full gate yerine geçmez.
- Host RPM Fusion FFmpeg smoke 23 PASS/0 SKIP; container libx264 ayrıca görüldü. Sonraki skip'i bildir.
- 2026-10-30 macOS full gate tarihsel; sonraki uzlaştırma kaynak değişikliğiyle bayat.

## Bilinen riskler
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
- Starlette httpx→httpx2 deprecation uyarısı: şimdi iş değil, bağımlılık bakımında ele alınacak.
