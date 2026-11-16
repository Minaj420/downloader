# Durum

Devam özeti; iş akışı `docs/WORKFLOW.md`. Son güncelleme: 2026-11-17.

## Şu an

- Mod: Normal, lead OpenCode; bekleyen Claude-only GPT review'u yok.
- M6 — Metadata ve Adlandırma: M6.4a–f dönüşüm kapsamı kapalı; M6 ve M6.5+ açık.
- Uzlaştırma yedek dalı korunur; push yalnız açık kullanıcı talebi ve hedefle.
- Onaylı sabit profillerin yetkili belgeye aktarımı bekliyor: MP3 V0, M4A AAC 256k,
  Opus 160k, WAV/FLAC 16-bit; MP4 x264 CRF20 medium + AAC 192k. Yeni ürün kararı değil.
- Onaylı, belgeye aktarılacak: videodan ses çıkarma yok; ses doğrudan ses akışından.

## Sıradaki iş

- M6.5b/c tekli ve collection remux + dar fallback uygulandı; uyarı katalogla atomik.
- Sıradaki bounded iş: remux durumunu PRODUCT_SPEC §8/§14 ve ARCHITECTURE §14/M6'da
  eşitle; sonra metadata/etiket kararlarını netleştir. M6.5 hâlâ açık.
- Etiketler ürün kararlarından, artwork açık kapak seçiminden sonra; M6.5 açık.

## Açık kararlar

- M6.5 etiket/kapak gömme biçimleri ve pipeline detayları; çoklu sanatçı,
  kısmi metadata yerleşimi ve disk öneki açık. M9 kalite/boyut sadeleştirmesi bekliyor.
- M6.9 MB/CAA yalnız aday zinciri; noncommercial/API koşulları ve kapak telifi ayrı.
  Açık kaynak dağıtım için preview/cache/embed hakları entegrasyon öncesi netleşmeli.
- Onaylı davranışlar: PRODUCT_SPEC §8–9/§13–16, ARCHITECTURE §14/§30–34/§59.
  Original WebM/Opus remux tekli/collection uygulandı; tagging ve artwork planlı.

## Açık bulgular

- should-fix: spec durumları bayat (PRODUCT_SPEC §8–9/§14, ARCHITECTURE §14/M6);
  remux durumu collection kapanışında; sabit profillerin yetkili belgeye aktarımı ayrı iş.
- should-fix: collection başarılı convert/caller-cancel testi sleep(0) ile deterministik değil;
  single başarılı convert+iptal ek testi eksik. Üretim hatası bulunmadı.
- should-fix: child hata/iptal staging temizliği yok; disk kullanımı artabilir.
- should-fix: boş kuyruk container SIGTERM kapanışı 10 sn'yi aştı; Fedora Python 3.14'te
  eski üç backend failure (os.close monkeypatch/pidfd) macOS gate ile doğrulanmış değil.
- should-fix: child Skip Existing Activity uyarısı yok; ayrı lifecycle/transaction işi.
- should-fix: modelsiz implement resume CLI hatasının belgelenmesi ve izleyici partial-line
  testinin deterministikliği açık; restart/model reload ve farklı-rol resume izolasyonu kanıtsız.
- should-fix: lead.md eski bootstrap referansı ayrı bakımda değerlendirilmeli.
- should-fix: geniş git diff/log izinleri --output seçeneğini teknik olarak dışlamıyor;
  yalnız salt-okunur biçimler kullanılır, config bakımı ayrı. gh PR wildcard'ları da repo/hedef/
  body-file sınırını sağlamaz; açık repo/base/head ve düz metin body kullanılmalı.
- Canlı slash yüklenmesi ve tüm ask/deny/yol dışı izin sınırları doğrulanmış değil.

## Son gate

- 2026-11-17 Fedora, M6.5c: prepared/collection/single/output-profile 857 PASS;
  hedefli Ruff/mypy PASS; full gate ve yeni gerçek FFmpeg smoke çalıştırılmadı.
- Önceki gerçek FFmpeg smoke (2026-11-16): 19 PASS + 4 SKIP (libx264 eksik).
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
