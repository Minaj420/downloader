# Durum

Güncel özet; iş akışı `docs/WORKFLOW.md`. Son güncelleme: 2026-10-29.

## Şu an

- M0–M5 kabul edildi (M5: playlist/album, `d37b78f`); child dosya izni artık `0644`.
- Kullanıcı dağıtım hedefini netleştirdi: uygulama açık kaynak dağıtılacak; bu, otomatik olarak
  ticari olmayan kullanım veya kapakların açık lisanslı olduğu anlamına gelmez.
- OpenCode: GPT-6.1 Sol lead/reviewer; Sonnet varsayılan, Opus uzman implementer; Luna mekanik/checker.
- Danışman/review Opus kalır. Lead uygulayıcıyı ve gerekçesini iş başında açıklar;
  model değişimi sessiz yapılmaz, implement resume açık --model ister.
- Otomatik izleme: `bash scripts/claude-watch.sh --follow-latest`; en yeni logu gösterir, yeni göreve
  geçer, result sonrası bekler. Claude çağrıları `--log` kullanmalı; Ctrl+C yalnız izleyiciyi kapatır.
- M6 Opus başlangıç/dönüşüm görüşleri alındı; M6.0 hazırlık tamam (`29206a9`).
- M6.1 (`d2c6667`): tekli analizde opsiyonel source_metadata, eski JSON uyumu; child metadata yok.
- M6.2 (`8a1c303`): bayt sınırlı klasör temizliği ve merkezi yol planlama; nested yazma aktif değil.
- M6.3 (`f32facd`): koleksiyon video NN - Başlık.ext, kalıcı source_order, numaralar sıkıştırılmaz;
  katalog başlığı/standalone/audio/completed işler değişmedi.
- M6.4a tamamlandı: onaylanan dönüşüm kapsamı PRODUCT_SPEC ve ARCHITECTURE'a işlendi;
  dönüşüm runtime/API/arayüzü henüz uygulanmadı. Sabit presetlerin kesin değerleri kilitlenmedi.
- İlk Sonnet belge çağrısı Bash grep izin reddiyle başarısız kaldı. Kullanıcı devam yetkisiyle
  mevcut çıktı incelendi; tek review turunda kalite iddiası ve fallback özeti düzeltildi.
  Aynı model açık resume: sonnet / claude-sonnet-5-5, success=true, permission_denials=false.
  GPT reviewer must-fix kalmadığını doğruladı; önceki başarısız çağrı başarılı sayılmadı.

## Sıradaki iş

1. M6.4b profil kalıcılığı/API: mevcut schema/migration/readers/testleri incele ve Opus'a danış;
   anlamlı API/kalıcı veri sözleşmesi nedeniyle uygulayıcı Opus. Eski kayıtlar Original kalmalı.
   Dönüşüm runtime'ı hazır değilken yeni çıktı seçimini yanlış biçimde çalıştırıp completed sayma.
2. Takip: M6.4c FFmpeg adaptör → M6.4d dönüşüm kaynak seçimi/
   pipeline+cancel/retry → M6.4e arayüz → M6.4f gerçek binary/çıktı smoke.
   M6.5 metadata/etiket pipeline bağlantısı →
   M6.6 müzik yolları/katalog → M6.7 kaynak kapak → M6.8 metadata/kapak önizleme-düzenleme →
   M6.9 harici kapak arama (haklar koşullu) → M6.10 kapanış gate. Başlıklar küçük işlere bölünebilir.
3. Lead: mevcut staging/yayınlama sınırlarını koru; kaynak alanlarını tahmin etmeden taşı.
   Playlist adı otomatik albüm değildir; albüm alanı tek başına küçük resmi güvenilir yapmaz.
   Kayıpsız remux kaliteyi korur; yeniden kodlama ve başlık temizliği kayıp riski taşır.
Kural: review ve kontroller sonrası görev başına otomatik commit; push yalnız açık taleple.

## Açık kararlar

- Kullanıcı: bu bir bağımsız converter değil, dönüşüm seçenekleri sunan indirme uygulaması;
  yeni indirmede seçilen desteklenen çıktı biçimi gerektiğinde dönüştürülerek üretilmeli.
  Original varsayılanı ile isteğe bağlı transcode ayrı: kaynak kalite kuralı transcode yasağı değil.
  Mevcut Library veya dosya yükleyerek dönüşüm bu istekle kapsama alınmadı.
- ONAYLI ilk ses seti: Original, MP3, WAV, M4A, Opus, FLAC; her biçimde sabit kalite profili,
  bitrate kaydırıcısı yok. Kesin codec/bitrate/preset değerleri sonraki teknik iş; henüz kilitlenmedi.
- ONAYLI video: Original + MP4 (H.264/AAC uyumlu, gerekirse transcode). Açık dönüşüm hedefi
  üretilemezse iş/child failed; farklı biçimde orijinal sessizce yayınlanıp completed sayılmaz.
  Etiket/kapak hatası hazır dönüştürülmüş çıktıyı dönüşüm öncesi biçime geri düşürmemeli.
- Teknik lead kararı: kaynak variant/kalite tercihi çıktı profilinden ayrı; seçim işte kalıcı,
  retry aynı seçimi kullanır, eski eksik profil Original olur. Şema/enum henüz kilitlenmedi.
  Ayrı converter/runner yok; mevcut FFmpeg/prepared_media/staging/storage sınırları korunacak.
  Açık dönüşüm hatası ile opsiyonel etiket/kapak fallback'i ayrı sözleşmeler olarak netleştirilecek.
  Fake argv testine ek gerçek codec/container doğrulaması planlandı; encoder/build garantisi yok.
- Onaylı M6: kayıpsız WebM/Opus → .opus remux; AAC/M4A korunur. Metadata uydurulmaz;
  sanatçı/albüm yoksa düz yerleşim, parça adı için kaynak başlığı. Özgün playlist sırası korunur,
  numaralar sıkıştırılmaz; otomatik başlık eki temizliği yok.
- Kapak önizlenir, son seçim kullanıcıda. Kullanıcı düşük kaliteli kaynak kapağına alternatif olarak
  düğmeyle harici veritabanlarında arama ve aday seçimi istiyor; sessiz arama/değiştirme yok.
- Etiket/kapak/remux hatasında güvenli orijinal + uyarı onaylandı; iptal/güvenlik/depolama/katalog
  hataları yutulmaz. Gömme biçimleri, ilk sağlayıcı ve harici kapak hakları henüz netleşecek.
- 2026-10-29 resmî web kontrolü: MB/CAA tek zincir, henüz yalnız aday. MB noncommercial arama
  ücretsiz/anahtarsız; User-Agent uygulama/sürüm/iletişim, en çok 1 istek/sn (IP dahil), 503 mümkün.
  Core CC0, ek veri CC BY-NC-SA 3.0; kapaklar bu lisanslarda değil. CAA'da belgeli hız sınırı yok,
  sınırsızlık garantisi değil. API erişimi veya kullanıcı seçimi kapak kullanım izni değildir.
  Preview/cache/embed hakları ve proje erişim şartları entegrasyondan önce netleşmeli; IA tam
  koşulları JS yüzünden okunamadı. MetaBrainz open-source yazarlarını nonprofit, bireysel kullanıcıyı
  personal tier'a yönlendiriyor; bu API anahtarı şartı değildir. Kapaklar yazılımla paketlenmez.
  Resmî kaynak bağlantıları PRODUCT_SPEC §14 / ARCHITECTURE §34'te; ek: help.archive.org/help/rights/.
- Çoklu sanatçı klasör adı, kısmi metadata yerleşimi ve disk öneki M6.2'de karara bağlanmadı.
- M9: video kalite basamaklarını sadeleştirme ve boyut gösterimi.
Kaynaklar: PRODUCT_SPEC §13–16, ARCHITECTURE §30–34 ve §59 M6.

## Açık bulgular

- should-fix: boş kuyrukta container SIGTERM sonrası 10 sn içinde kapanmadı; Podman SIGKILL kullandı.
- should-fix: Fedora Python 3.14'te mevcut 3 backend test hatası (aşağıda).
- should-fix: test_jobs.py iptal testi DOWNLOADING görünür görünmez staging.exists varsayıyor;
  claim commit'i mkdir'den önce olduğundan yarış var. M6.1 bu sırayı değiştirmedi; kapsam dışı.
- should-fix: collection child Skip Existing sonucunu Activity uyarısıyla göstermiyor; M6.3 mevcut
  skip/identity davranışını korudu. Activity/SSE/transaction sahipliği ayrı scoped iş gerektirir.
- Sonnet/Opus uygulama/test ve açık aynı-model resume görüldü; farklı-rol resume izolasyonu
  denenmedi. Dosya kapsamı prompt kuralıdır, teknik sandbox değildir. Native bg/attach yok.
- should-fix: WORKFLOW kullanımına, modelsiz implement resume'ın CLI hatası verdiği açıkça eklenebilir;
  aynı modelin açık yeniden seçimi zaten kuraldır. Gözlenen model yalnız init'ten, lead de doğrular.
- should-fix: izleyicinin partial-line testinde buffer adımları daha deterministik doğrulanabilir;
  reviewer uygulamada ilgili bir hata görmedi, otomatik takip için bloklamıyor.

## Son gate

- 2026-10-29 Fedora, M6.4a: ilk çağrı izin reddiyle FAIL; kullanıcı devamından sonra tek review
  düzeltmesi PASS, model eşleşti/izin reddi yok. Worker ve lead belge kontrolü; GPT review must-fix yok;
  lead diff --check PASS. Kod/test/config değişmedi; belgeye özel test tanımlı değil, test/full gate yok.
- 2026-10-29 Fedora Python 3.14.8, M6.3: worker ve lead child_storage+collection child/parent+
  storage_utils 321 PASS/3 FAIL (yalnız bilinen descriptor/pidfd testleri; deselect edilmedi).
  Aynı hata yolundaki await edilmemiş coroutine uyarısı korundu; ayrı child runner
  `-W error::RuntimeWarning` worker/lead 56 PASS. Hedefli beş dosya ruff/mypy ve diff --check PASS.
  GPT review must-fix yok; full gate/frontend çalıştırılmadı. Gerçek Retry endpoint'i bu işte
  ayrıca sınanmadı; yayın sonrası katalog hatası testi kontrollü reset ile yol tutarlılığını sınar.
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
- M6.2 containment yalnız planlama anında; nested yazma etkinleştirilmeden önce writer tarafında
  güvenli kök/parent doğrulaması zorunlu. Root içi symlink mevcut gibi kabul; TOCTOU koruması değil.
- Accepted: Delete File stat/unlink mikro-penceresi; bazı terminal parent error.retryable alanları;
  ertelenmiş cancel 404 ve cancel event temizliği. Elisa M4A açma sorunu oynatıcıya özgü.

## Sonraki fazlar

- Teknik: kapanış süresi, container user, yt-dlp sabitleme/görünürlük, Python 3.12 CI.
- Gerçek smoke: binary/ses/merge-video/playlist PASS; iptal ve yeniden başlatma henüz denenmedi.
- ARCHITECTURE bölme → M6 → M7 Plex → M8 cihaza indirme → M9 cilalama → v0.1 denetim.
