# Durum

Devam özeti; iş akışı `docs/WORKFLOW.md`.

## Şu an

- Mod: Normal, lead OpenCode; bekleyen Claude-only GPT review'u yok.
- M6 açık; Q0/18 kullanıcı Compose smoke PASS ve onayıyla kapandı. M6-Q genel kapanışı yok.
- Uzlaştırma yedek dalı korunur; push yalnız açık kullanıcı talebi ve hedefle.
- Onaylı sabit profillerin yetkili belgeye aktarımı bekliyor: MP3 V0, M4A AAC 256k,
  Opus 160k, WAV/FLAC 16-bit; MP4 x264 CRF20 medium + AAC 192k. Yeni ürün kararı değil.
- Onaylı, belgeye aktarılacak: videodan ses çıkarma yok; ses doğrudan ses akışından.

## Sıradaki iş

- Onaylı sıra: kapanış18 → teşhis19 → runtime6 → Q1/Q2 → çakışma3 → temizlik10/Q3 → M6.5.
- İş19: koleksiyon/Retry ve tekil CR smoke PASS; Q19a tekrar giderme/500 karakter sınırı için tekil Compose tekrarı bekler.
- M6.5 Mutagen eşleme/adapter ve pipeline bekler; metin → M6.6 grup/yollar → M6.7 kapak sırası korunur.
- M6.4/5 kapanışı yok; full gate ve değişen davranış için yeni kullanıcı Compose smoke'u gerekir.

## Açık kararlar

- M6.6 güvenilir albüm grubu/ön hazırlık ve çoklu album_artists klasör değeri; M6.7 cover.jpg seçimi açık.
- M6.9 yalnız seçim ekranından kullanıcı isterse; MB/CAA aday, hak/API/embed koşulları o işte netleşecek.
- Mutagen lisansı kişisel kullanıma engel değil; dağıtım uyumu notu sürer. M9 kalite/boyut bekler.
- S1 M7 hedef doğrulamasıyla onaylı; açılış yaz-sil probu/kalıcı hata bandı planlı; S2 v0.1 sonrası.
- Koleksiyon sabit tavan + öğe bazlı best fallback onaylı; tekil DELETE yok. Tema yeri M9 opsiyonel öneri.
- Onaylı Retry/retention, failed staging, seçici20 ve K3 yetkili belgelerde; K2 onaylı, bloklamaz/ertelenebilir.

## Açık bulgular

- should-fix: eski conversion durumları PRODUCT_SPEC §8–9 ve ARCHITECTURE M6
  kapanış paragrafında bayat; sabit profillerin yetkili belgeye aktarımı ayrı iş.
- should-fix: collection başarılı convert/caller-cancel testi sleep(0) ile deterministik değil;
  single başarılı convert+iptal ek testi eksik. Üretim hatası bulunmadı.
- Audit aktarımı alındı (ARCH §59): Settings → S1/M7; Library detay → M9-L; son seçim localStorage v0.1.
- Öncelikli: K1 Queue placeholder; K2 New Download gezinme/F5'te kayboluyor; draft restore uygulaması bekler.
- M6.5: UI hata ayrıntısı+maskeleme önerisi, failed0%/iç kod/satırlar arası boşluk; rozet/SSE ve N ağ hatasında durma ARCH §59.
- Öncelikli3/6/10/19 ve kullanıcı smoke/notları ARCH §59; network_error/deno bağı kanıtsız.
  Analiz25/+25/hepsi200 kararı kayıtlı, uygulanmadı; Private video seçilemez filtresi ayrı küçük iş bekler.
- Fedora Python 3.14 eski üç backend failure (os.close monkeypatch/pidfd) macOS gate ile doğrulanmadı.
- should-fix: child Skip Existing Activity uyarısı yok; ayrı lifecycle/transaction işi; analiz View Activity açık.
- should-fix: modelsiz implement resume CLI hatasının belgelenmesi ve izleyici partial-line
  testinin deterministikliği açık; restart/model reload ve farklı-rol resume izolasyonu kanıtsız.
- should-fix: lead.md eski bootstrap referansı ayrı bakımda değerlendirilmeli.
- should-fix: geniş git diff/log izinleri --output seçeneğini teknik olarak dışlamıyor;
  yalnız salt-okunur biçimler kullanılır, config bakımı ayrı. gh PR wildcard'ları da repo/hedef/
  body-file sınırını sağlamaz; açık repo/base/head ve düz metin body kullanılmalı.
- Canlı slash yüklenmesi ve tüm ask/deny/yol dışı izin sınırları doğrulanmış değil.
- Teknik takip: non-root UID/GID→M7/S1 ayrı Opus işi; yt-dlp sabitleme, Python3.12 CI ve ARCH bölme (§59).
## Son gate

- 2026-11-18 kullanıcı Compose Q19: koleksiyon log/başarı/Retry ve tekil CR PASS; tekrar/uzunluk son smoke'u bekler.
  Q19a lead pytest73/Ruff/mypy PASS; GPT review must-fix yok; lead Docker/full gate yapmadı.
- 2026-11-17 kullanıcı Fedora Compose Q18 PASS: single/collection5.5s, idle0.8s, Exit0/OOMfalse, exec doğrulandı.
- Single restart→Failed/Retry ~%4'ten devam PASS; bu kullanıcı Cancel→Retry değil, Q2 hâlâ bekler.
- Mix4 sabit seçim/completed korundu, auto-resume yok, Retry46→48; MKV VP9/Opus200s tam decode kullanıcı PASS.
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
- Accepted: HTTP5s istekleri iptal edebilir; Compose30s aşımı ve migration sırasında stop için crash recovery sınırı sürer.
- Starlette httpx→httpx2 deprecation uyarısı: şimdi iş değil, bağımlılık bakımında ele alınacak.
