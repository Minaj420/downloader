# Durum

Güncel özet; iş akışı `docs/WORKFLOW.md`. Son güncelleme: 2026-10-29.

## Şu an

- M0–M5 kabul edildi (M5: playlist/album, `d37b78f`); child dosya izni artık `0644`.
- OpenCode: GPT-6.1 Sol lead/reviewer; Sonnet varsayılan, Opus uzman implementer; Luna mekanik/checker.
- Danışman/review Opus kalır. Lead uygulayıcıyı ve gerekçesini iş başında açıklar;
  model değişimi sessiz yapılmaz, implement resume açık --model ister.
- İnce CLI adaptörü kuruldu: özel görev deposu, kilit ve test yürütücüsü yok; isteğe bağlı canlı log var.
- Gerçek salt-okunur Opus review ve aynı oturuma dönüş PASS: `claude-opus-5-5`, izin reddi yok.
- Opus önerisi: ince adaptörü koru; sonraki adım tek dosyalık uygulama + hedefli test, sonra aynı-rol resume.
- GPT ve Opus son review: must-fix yok. Yapılandırma için OpenCode yeniden başlatılmalı;
  mevcut reviewer oturumunun yeni modeli kullandığı iddia edilmez.
- M6 metadata/isimlendirme planlama aşamasında; uygulaması başlamadı.

## Sıradaki iş

1. Yeniden başlatılmış OpenCode'dan küçük, geri alınabilir bir gerçek Sonnet uygulama/test denemesi;
   sonra aynı modelle açık resume. Sonnet/Opus implementer canlı test döngüleri henüz denenmedi.
2. M6 öncesi Opus planlama görüşü ve kullanıcı ürün kararları. Görüş alınamazsa açık onay olmadan ilerleme.
3. Lead Opus önerisi/gerekçe/alternatif-risk ve kendi kararını burada özetler.
Kural: review ve kontroller sonrası görev başına otomatik commit; push yalnız açık taleple.

## Açık kararlar

- M6: ses biçimi (kaynak WebM/Opus → Opus çıkarma / M4A / MP3); eksik sanatçı/albüm/parça adları.
- Parça numarası kaynak sırası mı metadata mı; başlıktaki Official Video vb. ekler temizlensin mi?
- Kapak kaynağı/güven seviyesi; metadata ve kapak gömülecek biçimler ve hata davranışı.
- M9: video kalite basamaklarını sadeleştirme ve boyut gösterimi.
Kaynaklar: PRODUCT_SPEC §13–16, ARCHITECTURE §30–34 ve §59 M6.

## Açık bulgular

- should-fix: boş kuyrukta container SIGTERM sonrası 10 sn içinde kapanmadı; Podman SIGKILL kullandı.
- should-fix: Fedora Python 3.14'te mevcut 3 backend test hatası (aşağıda).
- Uygulayıcı dosya kapsamı prompt kuralıdır; gerçek implementer/test ve farklı-rol resume izolasyonu
  doğrulanmadı. Native bg/attach kullanılmıyor; izleme salt-okunur canlı günlük üzerinden.
- should-fix: WORKFLOW kullanımına, modelsiz implement resume'ın CLI hatası verdiği açıkça eklenebilir;
  aynı modelin açık yeniden seçimi zaten kuraldır. Gözlenen model yalnız init'ten, lead de doğrular.

## Son gate

- 2026-10-29 Fedora, model seçimi işi: lead pytest `test_claude_bridge.py` 32 PASS;
  test+adaptör+izleyici ruff PASS; hedefli mypy PASS; diff-check PASS. Full gate çalıştırılmadı.
- Önceki full gate: macOS backend ruff/mypy PASS, pytest 1466 PASS; frontend 147 test ve tüm gate'ler PASS.
- Önceki Fedora Python 3.14 full gate: ruff/mypy PASS, pytest 1462 PASS/1 skip/3 FAIL;
  `test_rejected_output_closes_all_walk_descriptors` os.close monkeypatch/pidfd çakışması.
  Frontend tümü PASS. Bu eski sonuçlar yeni test/araç ağacı için güncel full gate değildir.

## Bilinen riskler

- Claude tool izinleri OS sandbox değil; testler kod çalıştırır. Built-in plugin'ler CLI'da kalıyor;
  canlı review araçları yalnız Read/Grep/Glob, MCP/skills boş. Kota hatasında sessiz fallback yok.
- Canlı log kaynak metni içerebilir; ignored tmp altında. İzleyiciden çıkış işi durdurmaz;
  asıl lead çağrısının açık kalması gerekir, timeout/interrupt süreç davranışı tam doğrulanmadı.
- Container root; yt-dlp sabit/görünür değil; hedef filesystem hard link desteği istiyor.
- Accepted: Delete File stat/unlink mikro-penceresi; bazı terminal parent error.retryable alanları;
  ertelenmiş cancel 404 ve cancel event temizliği. Elisa M4A açma sorunu oynatıcıya özgü.

## Sonraki fazlar

- Teknik: kapanış süresi, container user, yt-dlp sabitleme/görünürlük, Python 3.12 CI.
- Gerçek smoke: binary/ses/merge-video/playlist PASS; iptal ve yeniden başlatma henüz denenmedi.
- ARCHITECTURE bölme → M6 → M7 Plex → M8 cihaza indirme → M9 cilalama → v0.1 denetim.
