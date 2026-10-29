# Durum

Güncel özet; iş akışı `docs/WORKFLOW.md`. Son güncelleme: 2026-10-29.

## Şu an

- M0–M5 kabul edildi (M5: playlist/album, `d37b78f`); child dosya izni artık `0644`.
- Kullanıcı dağıtım hedefini netleştirdi: uygulama açık kaynak dağıtılacak; bu, otomatik olarak
  ticari olmayan kullanım veya kapakların açık lisanslı olduğu anlamına gelmez.
- OpenCode: GPT-6.1 Sol lead/reviewer; Sonnet varsayılan, Opus uzman implementer; Luna mekanik/checker.
- Danışman/review Opus kalır. Lead uygulayıcıyı ve gerekçesini iş başında açıklar;
  model değişimi sessiz yapılmaz, implement resume açık --model ister.
- İnce CLI adaptörü kuruldu: özel görev deposu, kilit ve test yürütücüsü yok; isteğe bağlı canlı log var.
- Otomatik izleme: `bash scripts/claude-watch.sh --follow-latest`; en yeni logu gösterir, yeni göreve
  geçer, result sonrası bekler. Claude çağrıları `--log` kullanmalı; Ctrl+C yalnız izleyiciyi kapatır.
- Gerçek salt-okunur Opus review ve aynı oturuma dönüş PASS: `claude-opus-5-5`, izin reddi yok.
- Gerçek Sonnet uygulama/test döngüsü PASS: `claude-sonnet-5-5`, yalnız izinli test dosyası değişti;
  pytest/ruff/mypy komutlarını kendisi çalıştırdı, açık aynı-model resume takip çağrısı PASS, izin reddi yok.
- Otomatik izleyici Sonnet ile geliştirildi; bağımsız GPT review: must-fix yok;
  lead hedefli kontrolleri tekrar doğruladı. Uygulamanın indirme davranışı değişmedi.
- M6 başlangıç Opus görüşü alındı; requested opus / observed claude-opus-5-5 eşleşti,
  çağrı başarılı ve izin reddi yok. Kullanıcı tercihleri PRODUCT_SPEC ve ARCHITECTURE'a işlendi;
  M6.0 hazırlık tamamlandı (`29206a9`).
- M6.1 tamamlandı: tekli analiz POST/GET ve saklanan JSON'da opsiyonel source_metadata;
  yalnız açık kaynak alanları, bozuk alan izolasyonu, eski JSON uyumu. Collection parent None;
  child probe bağlantısı, frontend gösterimi, adlandırma, etiketleme ve kapak henüz uygulanmadı.
- M6.1 için Opus uygulama/test çağrısı doğrulandı: opus / claude-opus-5-5, başarı ve izin reddi yok;
  bağımsız GPT review must-fix yok. Kaynak model adı source-only provenance için yeterli;
  artist listeleri korunur, başlık/kanal/yükleme tarihi üzerinden metadata çıkarılmaz.

## Sıradaki iş

1. M6.2 merkezi güvenli dosya/klasör yolu oluşturucu; path güvenliği nedeniyle Opus.
   Uygulamadan önce mevcut storage containment ve testlerini incele, dar teknik kapsamı danış.
2. Takip: M6.3 playlist video adları → M6.4 FFmpeg etiket/remux → M6.5 pipeline bağlantısı →
   M6.6 müzik yolları/katalog → M6.7 kaynak kapak → M6.8 metadata/kapak önizleme-düzenleme →
   M6.9 harici kapak arama (haklar koşullu) → M6.10 kapanış gate. Başlıklar küçük işlere bölünebilir.
3. Lead: mevcut staging/yayınlama sınırlarını koru; kaynak alanlarını tahmin etmeden taşı.
   Playlist adı otomatik albüm değildir; albüm alanı tek başına küçük resmi güvenilir yapmaz.
   Kayıpsız remux kaliteyi korur; yeniden kodlama ve başlık temizliği kayıp riski taşır.
Kural: review ve kontroller sonrası görev başına otomatik commit; push yalnız açık taleple.

## Açık kararlar

- Onaylı M6: kayıpsız WebM/Opus → .opus remux; AAC/M4A korunur. Metadata uydurulmaz;
  sanatçı/albüm yoksa düz yerleşim, parça adı için kaynak başlığı. Özgün playlist sırası korunur,
  numaralar sıkıştırılmaz; otomatik başlık eki temizliği yok.
- Kapak önizlenir, son seçim kullanıcıda. Kullanıcı düşük kaliteli kaynak kapağına alternatif olarak
  düğmeyle harici veritabanlarında arama ve aday seçimi istiyor; sessiz arama/değiştirme yok.
- Etiket/kapak/remux hatasında güvenli orijinal + uyarı onaylandı; iptal/güvenlik/depolama/katalog
  hataları yutulmaz. Gömme biçimleri, ilk sağlayıcı ve harici kapak hakları henüz netleşecek.
- 2026-10-29 resmî web kontrolü: MusicBrainz ticari olmayan API araması ücretsiz/anahtarsız;
  User-Agent uygulama/sürüm/iletişim içermeli, uygulama/IP için en çok 1 istek/sn; 503 mümkün.
  Çekirdek veri CC0; ek veri CC BY-NC-SA 3.0. Açık kaynak dağıtım/ticari kullanım ayrı incelenmeli.
  MetaBrainz account-type açık kaynak yazarlarını nonprofit tier'a yönlendiriyor; tier 8 bireysel
  kullanıcılar için personal seçeneğini söylüyor (metabrainz.org/supporters/tiers/8 canlı okundu).
  Bu supporter yönlendirmesi API anahtarı zorunluluğu değildir. Yayın öncesi proje erişim koşullarını
  netleştir; sağlayıcı verisi/görsel lisanslarını kod lisansıyla aynı sayma, kapakları paketle dağıtma.
- MB + CAA ilk kaynak önerisi (tek zincir): CAA GET/HEAD, orijinal ve 250/500/1200 önizleme uçları;
  görsel istekleri CAA üzerinden başlamalı. Belgede CAA hız limiti yok; sınırsızlık/SLA garantisi değil.
- Uygunluk: API yönü olumlu; kapak telifleri sahiplerinde, MB lisansı görselleri kapsamaz.
  Cache/önizleme/gömme için genel hak izni doğrulanmadı; bu alt işin yazımı öncesi netleştir.
  IA tam koşulları JS nedeniyle okunamadı; resmî Rights yardım sayfası ihlalsiz kullanım sorumluluğunu
  kullanıcıya bırakıyor. Anahtar gerektirmemesi veya kullanıcı seçimi telif izni yerine geçmez.
  Kaynaklar: musicbrainz.org/doc/{MusicBrainz_API,MusicBrainz_API/Rate_Limiting,About/Data_License,
  MusicBrainz_Database,Cover_Art_Archive,Cover_Art_Archive/API}; coverartarchive.org;
  help.archive.org/help/rights/; metabrainz.org/supporters/account-type.
- M9: video kalite basamaklarını sadeleştirme ve boyut gösterimi.
Kaynaklar: PRODUCT_SPEC §13–16, ARCHITECTURE §30–34 ve §59 M6.

## Açık bulgular

- should-fix: boş kuyrukta container SIGTERM sonrası 10 sn içinde kapanmadı; Podman SIGKILL kullandı.
- should-fix: Fedora Python 3.14'te mevcut 3 backend test hatası (aşağıda).
- should-fix: test_jobs.py iptal testi DOWNLOADING görünür görünmez staging.exists varsayıyor;
  claim commit'i mkdir'den önce olduğundan yarış var. M6.1 bu sırayı değiştirmedi; kapsam dışı.
- Uygulayıcı dosya kapsamı prompt kuralıdır. Sonnet uygulama/test ve aynı-rol oturum dönüşü görüldü;
  Opus uygulama/test de görüldü; farklı-rol resume izolasyonu denenmedi. Native bg/attach yok.
- should-fix: WORKFLOW kullanımına, modelsiz implement resume'ın CLI hatası verdiği açıkça eklenebilir;
  aynı modelin açık yeniden seçimi zaten kuraldır. Gözlenen model yalnız init'ten, lead de doğrular.
- should-fix: izleyicinin partial-line testinde buffer adımları daha deterministik doğrulanabilir;
  reviewer uygulamada ilgili bir hata görmedi, otomatik takip için bloklamıyor.

## Son gate

- 2026-10-29 Fedora Python 3.14.8, M6.1: worker hedefli normalizer+API+jobs 281 PASS;
  lead tekrarı 280 PASS/1 FAIL (yukarıdaki iptal testi yarışı), ayrı diagnostic 1 PASS.
  Lead normalizer+API ayrı koşu 208 PASS; worker/lead dört değişen dosya ruff ve mypy PASS.
  GPT review must-fix yok; iptal testi should-fix. Full gate/frontend bu işte çalıştırılmadı.
- 2026-10-29 Fedora, kapak sağlayıcı uygunluğu: resmî web belgeleri incelendi, API/image smoke yok;
  uygulama/test/config değişmedi; hedefli test/full gate yok. IA tam koşulları eksik doğrulandı.
- 2026-10-29 Fedora, M6 belge eşitleme: Sonnet/claude-sonnet-5-5 eşleşti, izin reddi yok;
  worker ve lead belge tutarlılık kontrolü yaptı, GPT review must-fix yok, lead diff --check PASS.
  Belgeye özel otomatik test tanımlı değil; test/full gate çalıştırılmadı, runtime değişmedi.
- 2026-10-29 Fedora, otomatik izleme: worker ve lead pytest `test_claude_bridge.py` 43 PASS;
  test+izleyici ruff ve hedefli mypy PASS. Önceki model seçimi işinde adaptör ruff PASS.
  Bu çağrıda full gate çalıştırılmadı; test değişikliği bağımsız GPT review'dan geçti.
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
