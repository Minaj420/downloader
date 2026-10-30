# Durum

Güncel özet; iş akışı `docs/WORKFLOW.md`. Son güncelleme: 2026-10-30.

## Şu an

- Mod: Normal; lead OpenCode. Kullanıcı Claude tarafında `/normal-mod` uyguladı;
  `.claude/settings.json` normal mod kaynağıyla eşleşiyor (2026-10-30 doğrulandı).
  Önceki Claude-only dönemindeki commit'ler: `f926a63`
  (yalnız mod altyapısı), M6.4c (`dc0142b`), M6.4d-d1 (`9ee4c3f`). M6.4c FFmpeg
  adaptörü ve M6.4d-d1 dönüşüm çekirdeğinin bağımsız GPT review'u 2026-10-30 tamamlandı:
  must-fix yok; ikinci iptal sonrası kill/wait yaşam döngüsü should-fix olarak aşağıda.
  Claude-only dönemindeki tek düzeltme turu (`83fbcd3`) incelemeye dahil; tur sıfırlanmadı.
- Bu çağrı yalnız bekleyen GPT incelemesini kapattı; production/test/config değişmedi.
  Kullanıcı diğer bilgisayardaki çalışmaları push edip burada pull yaptı;
  inceleme başlangıcında `main` kayıtlı `origin/main` ile eşit, tracked ağaç temizdi.
  Dört eski `.opencode/archive/_*` untracked dosya korundu; bu çağrıda push yok.
- M6.4c/d1 review düzeltme turu (kullanıcı onaylı, aynı Opus session): mp4 transcode tek sayılı boyut için
  `-vf scale=trunc(iw/2)*2:trunc(ih/2)*2` (copy_video'da yok); `-id3v2_version`/`-rf64` container grubunda
  (copy_audio'da da kalır); convert argv'sine `-nostats` (merge değişmedi); convert'te 0 bayt/eksik çıktı
  FAILED; terminate sırasında ikinci iptalde kill + çıktı silme (finally). Argv eklemeleri kullanıcı onaylı.
  Reddedilen: muxed kaynaktan ses çıkarma bulgusu (ürün kararı: yok). Kalan: codec etiketi kaba (aşağıda).
- M6.4d Opus görüşü alındı; dört dilim: d1 ortak çekirdek (bitti) → d2 tekli runner (Sonnet) → d3 koleksiyon
  child (Opus) → d4 API kapısı (en son; önce runner, sonra API). Kullanıcı onayı: videodan ses çıkarma yok
  (ses modu ses akışını doğrudan indirir); dosya zaten varsa dönüşümlü iş de mevcut kopyayı tutar (Skip Existing).
- M6.4d-d1 tamamlandı: `conversion_plan.plan_conversion` (yalnız tam codec eşleşmesinde copy; mp4 video yalnız
  h264, ses aac; wav/flac asla copy; bilinmeyen codec transcode; uyumsuz kaynak indirme öncesi INVALID_PROFILE/
  INVALID_VARIANT), `prepare_media(output=...)` (plan indirmeden önce, `converted.<uzantı>`, `before_merge`
  hook'u en fazla bir kez), `ffmpeg_conversion_timeout_seconds=3600` config/main bağlı. `output=None` yolu
  değişmedi. Runner/API kapıları aynen duruyor: dönüşüm henüz canlı işte çalışmıyor.
- M6.4c tamamlandı: `FFmpegProcessor.convert` tek profil tablosuyla mp3/wav/m4a/opus/flac/mp4; ortak `_run`,
  başarısızlık/timeout/iptalde yalnız kendi çıktısını silme, `input==output` ve uzantı `ValueError`,
  ayrı conversion timeout (3600 sn). Aktivasyon yok; runner/config bağlanmadı, M6.4b kapısı duruyor.
  Onaylı değerler: MP3 V0, M4A AAC 256k, Opus 160k, WAV/FLAC 16-bit, MP4 x264 CRF 20 medium + AAC 192k.
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
- M6.4a (`2e9383a`): onaylı dönüşüm kapsamı belgelendi. İlk Sonnet izin reddi FAIL olarak kaldı;
  kullanıcı devam yetkisi sonrası aynı-model tek review düzeltmesi kabul edildi.
- M6.4b tamamlandı: migration 0007 ortak nullable output_profile; create/read DTO/API temeli,
  eski/null kayıtlar Original, retry seçimi korur. Test kapsamı kullanıcı onayıyla genişletildi.
  Opus ve açık --model opus aynı-rol resume: claude-opus-5-5, başarı ve izin reddi yok;
  bağımsız GPT review must-fix yok, review rework gerekmedi.
- Dönüşüm motoru henüz yok: API non-Original create yan etkisiz INVALID_PROFILE;
  kayıtlı non-Original runner'da medya I/O öncesi reddedilir. Arayüz/preset/FFmpeg aktivasyonu yok.

## Sıradaki iş

M6.4c/d1 bağımsız GPT incelemesi tamamlandı; sonraki `/continue` tekli runner işini ele alır.

1. M6.4d-d2 (Sonnet): tekli runner. `require_runnable_output_profile` yerine fail-closed çözümleyici
   (bilinmeyen/türle uyumsuz profil medya I/O öncesi INVALID_PROFILE), `prepare_media(output=...)` bağlantısı,
   katalog codec/bitrate çıktıdan (`ConversionPlan.output_codec/keep_source_bitrate`; Skip Existing'de None),
   FFmpegError eşleme (BINARY_MISSING→FFMPEG_MISSING retry yok; FAILED/TIMEOUT→POST_PROCESSING_FAILED),
   Activity "Converting to ..." mesajı, iptal ve retry testleri. Sonra d3 child (tek POST_PROCESSING geçişi:
   child CAS aynı durumu tekrar kabul etmiyor; stage_child_completion katalog override), en son d4 API kapısı.
2. Takip: M6.4e arayüz → M6.4f gerçek binary/çıktı smoke (encoder var/yok, ffprobe codec/faststart,
   gerçek iptalde artık süreç yok; ffmpeg yoksa skip).
   M6.5 metadata/etiket pipeline bağlantısı →
   M6.6 müzik yolları/katalog → M6.7 kaynak kapak → M6.8 metadata/kapak önizleme-düzenleme →
   M6.9 harici kapak arama (haklar koşullu) → M6.10 kapanış gate. Başlıklar küçük işlere bölünebilir.
3. Lead: mevcut staging/yayınlama sınırlarını koru; kaynak alanlarını tahmin etmeden taşı.
   Playlist adı otomatik albüm değildir; albüm alanı tek başına küçük resmi güvenilir yapmaz.
   Kayıpsız remux kaliteyi korur; yeniden kodlama ve başlık temizliği kayıp riski taşır.
Kural: review ve kontroller sonrası görev başına otomatik commit; push yalnız açık taleple.

## Açık kararlar

- İndirme sırasında seçilen desteklenen çıktı gerektiğinde dönüştürülür; ayrı converter,
  dosya yükleme, mevcut Library dönüşümü ve koleksiyonda parça başına profil kapsam dışı.
- ONAYLI ilk ses seti: Original, MP3, WAV, M4A, Opus, FLAC; her biçimde sabit kalite profili,
  bitrate kaydırıcısı yok. Kesin codec/bitrate/preset değerleri sonraki teknik iş; henüz kilitlenmedi.
- ONAYLI video: Original + MP4 (H.264/AAC uyumlu, gerekirse transcode). Açık dönüşüm hedefi
  üretilemezse iş/child failed; farklı biçimde orijinal sessizce yayınlanıp completed sayılmaz.
  Etiket/kapak hatası hazır dönüştürülmüş çıktıyı dönüşüm öncesi biçime geri düşürmemeli.
- Teknik lead kararı: kaynak variant/kalite tercihi çıktı profilinden ayrı; seçim işte kalıcı,
  retry aynı seçimi kullanır, eski eksik profil Original olur. M6.4b ortak nullable
  download_jobs.output_profile String(16), API/DTO original/mp3/wav/m4a/opus/flac/mp4 seçildi.
  CollectionProfile native/max_height olarak kalır; ikinci çıktı alanı ve child profili yok.
  Dönüşüm aktivasyonu M6.4c/d öncesi kapalı; API non-Original create INVALID_PROFILE, runner
  kayıtlı non-Original/bozuk profili probe/download öncesi reddeder; sözleşme review'dan geçti.
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
- M6.9 sağlayıcı MB/CAA tek zincir, yalnız aday. 2026-10-29 resmî kontrol: MB noncommercial
  anahtarsız/ücretsiz, User-Agent+1 istek/sn (IP dahil); metadata core CC0/ek CC BY-NC-SA 3.0,
  kapaklar bu lisanslarda değil. Preview/cache/embed ve açık kaynak proje erişim şartları
  entegrasyondan önce netleşmeli; IA tam koşulları okunamadı. API erişimi telif izni değildir.
  Kapaklar yazılımla paketlenmez; kaynaklar PRODUCT_SPEC §14 / ARCHITECTURE §34'te.
- Çoklu sanatçı klasör adı, kısmi metadata yerleşimi ve disk öneki M6.2'de karara bağlanmadı.
- M9: video kalite basamaklarını sadeleştirme ve boyut gösterimi.
Kaynaklar: PRODUCT_SPEC §13–16, ARCHITECTURE §30–34 ve §59 M6.

## Açık bulgular

- should-fix (M6.4c/d1 GPT review): FFmpeg `_terminate` ikinci iptalde kill gönderiyor ancak
  process.wait tamamlanmadan dönüyor; kalıcı çalışan süreç kanıtı yok, adaptör dönüşünde reap
  garantisi eksik. Ayrı scoped takipte iptale dayanıklı wait ve adaptör dönüşünde returncode testi.
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

- 2026-10-30 macOS Python 3.14.9, M6.4c/d1 bağımsız GPT review: must-fix yok, bir should-fix.
  Lead hedefli ffmpeg/conversion_plan/prepared_media/child runner/dispatch/jobs/output_profiles:
  376 PASS, iki deprecation uyarısı; hedefli Ruff ve Mypy (9 dosya) PASS.
  Kaynak değişmedi; full gate/frontend ve gerçek binary smoke çalıştırılmadı.
- 2026-10-30 Fedora Python 3.14.8, M6.4c/d1 review düzeltmesi, lead: ffmpeg/prepared_media/conversion_plan/
  child runner/dispatch/jobs/output_profiles 7 test dosyası 376 PASS (worker); lead tekrarında 375 PASS +
  1 bilinen yarış (`test_cancel_running_job_terminates_and_cleans_staging`; düzeltme öncesi HEAD'de de 6
  denemede 2 düştü, düzeltmeyle 0/6; aşağıdaki mevcut should-fix). Hedefli ruff/mypy (4 dosya) ve diff --check PASS.
- 2026-10-30 Fedora Python 3.14.8, M6.4d-d1 worker ve lead: conversion_plan/prepared_media/ffmpeg adaptörü/
  child runner/dispatch/jobs/output_profiles 7 test dosyası 371 PASS; hedefli ruff, mypy (7 dosya) ve
  diff --check PASS. O tarihte GPT review bekliyordu; güncel inceleme yukarıda. Full gate/frontend çalıştırılmadı.
- 2026-10-30 Fedora Python 3.14.8, M6.4c worker ve lead: ffmpeg/prepared_media/collection runner+dispatch
  4 test dosyası 136 PASS (52 adaptör testi); hedefli ruff, mypy ve diff --check PASS. Opus review
  yok (Opus yazdı); o tarihte GPT review bekliyordu, güncel inceleme yukarıda. Full gate/frontend çalıştırılmadı.
- 2026-10-30 Fedora Python 3.14.8, M6.4b worker ve lead: hedefli 12 test dosyası 847 PASS
  (100 yeni profil testi dahil; migration/API/runner/retry/recovery/katalog). Hedefli ruff 18 dosya,
  mypy 17 dosya ve diff --check PASS. GPT review must-fix yok. Full gate/frontend çalıştırılmadı.
  Library migration beklentisi kullanıcı onayıyla 0007 oldu; temp SQLite upgrade/downgrade doğrulandı.
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
- Accepted (M6.4c/d1, M6.4f'te ffprobe veya ham codec dizesiyle ele alınacak): normalizer `mp4a.*`→aac ve
  `avc1/avc3.*`→h264 etiketleri kaba; `mp4a.69` (MP3) AAC sayılıp copy edilebilir (muxer hatası), H.264 High 10/
  4:4:4/interlaced copy edilip oynatıcıda açılmayan "uyumlu MP4" üretebilir. Ayrıca conversion timeout
  süreden bağımsız sabit 3600 sn ve alt sınır doğrulaması yok (düşük etki).
- Accepted (M6.4b tasarım): elle bozulmuş bilinmeyen output profili DTO/list/SSE doğrulamasını
  ve ilk snapshot nedeniyle normal failed sonlandırmayı bozabilir; Original'a sessiz düşüş yok.
- Accepted: Delete File stat/unlink mikro-penceresi; bazı terminal parent error.retryable alanları;
  ertelenmiş cancel 404 ve cancel event temizliği. Elisa M4A açma sorunu oynatıcıya özgü.

## Sonraki fazlar

- Teknik: kapanış süresi, container user, yt-dlp sabitleme/görünürlük, Python 3.12 CI.
- Gerçek smoke: binary/ses/merge-video/playlist PASS; iptal ve yeniden başlatma henüz denenmedi.
- ARCHITECTURE bölme → M6 → M7 Plex → M8 cihaza indirme → M9 cilalama → v0.1 denetim.
