# Durum

Devam özeti; iş akışı `docs/WORKFLOW.md`. Son güncelleme: 2026-11-13.

## Şu an

- Mod: Normal, lead OpenCode; bekleyen Claude-only GPT review'u yok.
- M6 — Metadata ve Adlandırma: M6.4a–f dönüşüm kapsamı kapalı; M6 ve M6.5+ açık.
- Eski ayrışmış yerel main üzerinden geliştirme yok; yedek dal korunmalı.
  Çıplak push yok; açık taleple yalnız hedefli push (bu dal için origin reconcile/conversion).
- Onaylı sabit profillerin yetkili belgeye aktarımı bekliyor: MP3 V0, M4A AAC 256k,
  Opus 160k, WAV/FLAC 16-bit; MP4 x264 CRF20 medium + AAC 192k. Yeni ürün kararı değil.
- Onaylı, belgeye aktarılacak: videodan ses çıkarma yok; ses doğrudan ses akışından.

## Sıradaki iş

- WF-S1 sonrasındaki 3–4 tamamlanan işten sonra STATE satır sayısını yeniden ölç;
  yeniden şişerse hook o zaman kullanıcıyla değerlendirilecek; şimdi hook yok.
- M6.5 — Metadata/etiket pipeline bağlantısı: önce Opus görüşüyle küçük kapsama böl;
  Original Opus remux ve fallback sınırlarını netleştir. Sonra M6.6–M6.10.

## Açık kararlar

- M6.5 etiket/kapak gömme biçimleri ve pipeline detayları; çoklu sanatçı,
  kısmi metadata yerleşimi ve disk öneki açık. M9 kalite/boyut sadeleştirmesi bekliyor.
- M6.9 MB/CAA yalnız aday zinciri; noncommercial/API koşulları ve kapak telifi ayrı.
  Açık kaynak dağıtım için preview/cache/embed hakları entegrasyon öncesi netleşmeli.
- Onaylı davranışlar: PRODUCT_SPEC §8–9/§13–16, ARCHITECTURE §14/§30–34/§59.
  Original WebM/Opus remux, tagging ve artwork hâlâ planlı; uygulanmış sayılmaz.

## Açık bulgular

- should-fix: spec durumları bayat (PRODUCT_SPEC §8–9, ARCHITECTURE M6 kapanış paragrafı);
  sabit profil değerlerinin yetkili belgeye aktarımı ayrı iş; WF-S1 bu belgeleri değiştirmez.
- should-fix: collection başarılı convert/caller-cancel testi sleep(0) ile deterministik değil;
  single başarılı convert+iptal ek testi eksik. Üretim hatası bulunmadı.
- should-fix: child hata/iptal staging temizliği yok; disk kullanımı artabilir.
- should-fix: boş kuyruk container SIGTERM kapanışı 10 sn'yi aştı; Fedora Python 3.14'te
  eski üç backend failure (os.close monkeypatch/pidfd) macOS gate ile doğrulanmış değil.
- should-fix: child Skip Existing Activity uyarısı yok; ayrı lifecycle/transaction işi.
- should-fix: modelsiz implement resume CLI hatasının belgelenmesi ve izleyici partial-line
  testinin deterministikliği açık; restart/model reload ve farklı-rol resume izolasyonu kanıtsız.
- should-fix: Claude-only başlangıç kapısına açık yönlendirme WF-S1 dışında, değiştirilmedi.
- should-fix: lead.md eski bootstrap referansı ayrı bakımda değerlendirilmeli.
- should-fix: geniş git diff/log izinleri --output seçeneğini teknik olarak dışlamıyor;
  yalnız salt-okunur biçimler kullanılır, config bakımı ayrı. gh PR wildcard'ları da repo/hedef/
  body-file sınırını sağlamaz; açık repo/base/head ve düz metin body kullanılmalı.
- Canlı slash yüklenmesi ve tüm ask/deny/yol dışı izin sınırları doğrulanmış değil.

## Son gate

- 2026-11-13 Fedora, WF-S1: statik kapsam/satır kontrolü ve diff --check PASS; uygulama gate'i yok.
- Uygulama için son ilgili hedefli kontrol (2026-11-10 Fedora): 237 PASS + 4 SKIP
  (libx264 yok), jobs/collection_retry 378 PASS; hedefli Ruff/mypy PASS. Atlananlar doğrulanmadı.
- 2026-10-30 macOS full gate tarihsel; sonraki uzlaştırma kaynak değişikliğiyle bayat.

## Bilinen riskler

- Accepted: private tek yazıcılı staging lstat/FFmpeg açılışı mikro yarışı sürer;
  eski token'lı çıktılar cleanup'a kadar disk kullanır. Mutlak race koruması değil.
- Accepted: kill sonrası 5 sn reap + en fazla 1 sn waiter drain; D-state garantisi yok,
  ek iptaller özgün kesintiyi değiştirmez; process-group kapsam dışı.
- Accepted: eski snapshot AAC/profil bilgisi düzeltilmedi; MPEG 69/6b/40.34 layer belirsiz;
  eksik/bozuk/diğer H.264 ve interlaced copy her cihazda uyumlu olmayabilir.
  Kaynak etiketi doğrulanmaz; timeout 3600 sn. Bozuk profil DTO/SSE'yi bozabilir; sessiz Original fallback yok.
- Gerçek smoke adapter/planner düzeyinde; runner/storage fake binary testli.
  Gerçek ağ dönüşüm uçtan uca/restart smoke yok; adaptörün gerçek iptali testli.
- M6.2 containment planlama anında; nested yazmadan önce writer kök/parent doğrulamalı;
  root içi symlink davranışı TOCTOU koruması değil. Container root, yt-dlp pinsiz; hard link gerekli.
- Claude izinleri OS sandbox değil; built-in plugin'ler kalır, log kaynak metni içerebilir;
  izleyiciyi kapatmak işi durdurmaz, ana çağrı açık kalmalı.
- Accepted: Delete File stat/unlink penceresi, bazı terminal parent retryable alanları,
  ertelenmiş cancel 404/event temizliği; Elisa M4A sorunu oynatıcıya özgü.

## Sonraki fazlar

- M6.6 müzik yolları/katalog → M6.7 kaynak kapak → M6.8 önizleme/düzenleme →
  M6.9 harici kapak (haklar koşullu) → M6.10 kapanış → M7 Plex → M8 cihaza indirme → M9 → v0.1.
- Teknik: kapanış süresi, container user, yt-dlp sabitleme/görünürlük, Python 3.12 CI;
  ARCHITECTURE bölme ayrı iş.
