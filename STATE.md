# Durum

Güncel özet; iş akışı `docs/WORKFLOW.md`. Son güncelleme: 2026-10-30.

## Şu an

- WF-R1-smoke TAMAMLANDI: Opus görüşü alındı (requested Opus / observed
  claude-opus-5-5 eşleşti; başarı, izin reddi yok). Yeni GPT reviewer ayrı doğrudan
  `git status --short`, `git diff --check` ve reviewer tanımını Read ile okuma
  adımlarını başarılı raporladı; must-fix yok. Lead status/diff --check tekrarında
  yalnız bilinen dört untracked arşiv ve temiz diff gördü. Reviewer araç argümanları
  child raporundan görüldü; bağımsız ham child tool trace/exit kodu sunulmadı.
  Dar davranış smoke'u tamam; restart, runtime talimat yüklenmesi/model reload,
  ret yolu ve genel izolasyon kanıtlanmadı. Ürün/config değişmedi; yalnız STATE.
- Workflow bakımı → WF-R1 — Reviewer komut sözleşmesi TAMAMLANDI. Opus danışmanlığı
  alındı (requested Opus / observed claude-opus-5-5, başarı ve izin reddi yok);
  Luna yalnız sabit prose metnini yerleştirdi. Reviewer frontmatter, izinler ve model
  değişmedi; docs/WORKFLOW.md ve .opencode/agents/reviewer.md güncellendi.
  Bağımsız GPT review: must-fix yok. Worker Read ve lead diff/`git diff --check` PASS.
  Opus son review wrapper çağrısı shell reddiyle başlamadı; tekrar/aşma yapılmadı.
  Kullanıcı 2026-10-30 yalnız bu dar prose işi için ek Opus review olmadan kapanışı
  açıkça onayladı; genel review kuralı değiştirilmedi, Opus review PASS sayılmadı.
  O kapanışta restart sonrası smoke bekliyordu; dar takip smoke'u yukarıda tamamlandı.
  Talimat değişikliği teknik garanti değil. Kapsam iki dosya + STATE; push yok.
- M6 — Metadata ve Adlandırma → M6.4d-d3 — Koleksiyon child dönüşüm bağlantısı TAMAMLANDI.
  Opus danışmanlığı alındı; uygulayıcı requested Opus / observed claude-opus-5-5 eşleşti,
  başarı ve izin reddi yok. Bağımsız GPT review tamam: must-fix yok, bir test should-fix.
  Worker ve lead: hedefli 11 dosyada 885 pytest PASS, altı dosyada Ruff/Mypy PASS;
  lead diff --check temiz. Review düzeltme turu kullanılmadı.
  Child dönüşümü tek POST_PROCESSING CAS ile bağlı; plan medya I/O öncesi doğrulanır,
  katalog codec/bitrate plandan, dönüşümlü Skip Existing için ikisi None.
  İptal/retry/recovery testli; API create kapısı ve arayüz kapalı kalır.
  İlk review çağrısı komut biçimi nedeniyle reddedildi; kullanıcı düzeltmesi sonrası
  aynı reviewer ayrı doğrudan git status/diff ve Read ile tamamladı. Config/izin değişmedi.
  Kapsam altı uygulama/test dosyası ve STATE; dört eski untracked arşiv korunur, push yok.
- Kalıcı kullanıcı iletişim tercihi: ilerleme ve sıradaki iş özetlerinde her zaman
  milestone numarası/adı, alt iş kodu ve kısa açıklamayı belirt. Örnek:
  M6 — Metadata ve Adlandırma → M6.4d-d3 — Koleksiyon indirmelerine dönüşüm bağlantısı.
- M6.4d-d2 TAMAMLANDI: Opus görüşü sonrası Sonnet tekli runner dönüşümünü bağladı;
  requested Sonnet / observed claude-sonnet-5-5 eşleşti. Bağımsız GPT review tamam, must-fix yok.
  Lead macOS doğrulaması: 430 test, hedefli Ruff/Mypy PASS. Review düzeltme turu gerekmedi.
  Bilinen profillerde plan staging/medya I/O öncesi doğrulanır; katalog codec/bitrate çıktı planından,
  dönüşümlü Skip Existing'de ikisi None; iptal/hata/retry/recovery yolları testli.
  D2 kapanışında API non-Original create ve collection child kapıları kapalıydı;
  child bağlantısı d3 ile tamamlandı; arayüz aktivasyonu yok.
  test_jobs.py staging beklemesi kullanıcı onayıyla kapsama alındı; test yarışı çözüldü.
  Reviewer izin engeli teşhis edildi: git --no-pager ve zincirli cat izin kalıbı dışında.
  Kullanıcının devam onayıyla ayrı git diff/status ve Read kullanıldı; izin/config değişmedi.
  Kapanış kapsamı altı uygulama/test dosyası ve STATE; dört eski untracked arşiv korunur, push yok.
- Mod: Normal; lead OpenCode. Kullanıcı Claude tarafında `/normal-mod` uyguladı;
  `.claude/settings.json` normal mod kaynağıyla eşleşiyor (2026-10-30 doğrulandı).
  Önceki Claude-only dönemindeki commit'ler: `f926a63`
  (yalnız mod altyapısı), M6.4c (`dc0142b`), M6.4d-d1 (`9ee4c3f`). M6.4c FFmpeg
  adaptörü ve M6.4d-d1 dönüşüm çekirdeğinin bağımsız GPT review'u 2026-10-30 tamamlandı:
  must-fix yok; ikinci iptal sonrası kill/wait yaşam döngüsü should-fix olarak aşağıda.
  Claude-only dönemindeki tek düzeltme turu (`83fbcd3`) incelemeye dahil; tur sıfırlanmadı.
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
  değişmedi. Sonraki d2 tekli runner bağlantısı tamam; API/child kapıları halen kapalı.
- M6.4c tamamlandı: `FFmpegProcessor.convert` tek profil tablosuyla mp3/wav/m4a/opus/flac/mp4; ortak `_run`,
  başarısızlık/timeout/iptalde yalnız kendi çıktısını silme, `input==output` ve uzantı `ValueError`,
  ayrı conversion timeout (3600 sn). Config ve tekli runner sonradan bağlandı; API/child kapıları duruyor.
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
- API non-Original create halen yan etkisiz INVALID_PROFILE; kayıtlı bilinen non-Original
  tekli işler ve koleksiyon child işleri dönüşebilir. Arayüz aktivasyonu henüz yok.

## Sıradaki iş

WF-R1 ve dar reviewer smoke takibi tamamlandı; restart/model reload kanıtlanmadı.
M6.4d-d3 tamamlandı; sıradaki iş d4 (bu çağrıda başlanmadı).

1. M6.4d-d4 — API kapısı: runner bağlantıları tamamlandıktan sonra non-Original create
   desteğini aç; d4 henüz başlamadı. Arayüz ayrı M6.4e işi.
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
   API non-Original create INVALID_PROFILE; tekli ve child runner bilinen uyumlu profilleri
   dönüştürür. Bozuk profil DTO riski aşağıda; sessiz Original fallback yok.
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

- should-fix (d3 GPT review): test_collection_conversion.py caller iptaliyle başarılı
  dönüşün çakışması testinde yayınlama/katalog oluşmadığı doğrulanıyor, ancak sleep(0)
  fake convert başarıyla dönmeden iptale izin verebilir. Deterministik başarılı dönüş
  ve ayrıca dönüşün tamamlandığını assert etme ayrı scoped takip; üretim hatası bulunmadı.
- should-fix: child hata/iptal sonrası staging temizliği mevcut akışta yapılmıyor;
  dönüşüm ara dosyaları disk kullanımını artırabilir. D3 kapsamına alınmadı.
- should-fix (M6.4c/d1 GPT review): FFmpeg `_terminate` ikinci iptalde kill gönderiyor ancak
  process.wait tamamlanmadan dönüyor; kalıcı çalışan süreç kanıtı yok, adaptör dönüşünde reap
  garantisi eksik. Ayrı scoped takipte iptale dayanıklı wait ve adaptör dönüşünde returncode testi.
- should-fix: boş kuyrukta container SIGTERM sonrası 10 sn içinde kapanmadı; Podman SIGKILL kullandı.
- should-fix: Fedora Python 3.14'te mevcut 3 backend test hatası (aşağıda).
- should-fix (d2 GPT review): dönüşüm başarıyla dönerken iptal gelmesi senaryosu için ek test;
  mevcut test yalnız FFmpegError yolunu kapsıyor. Kodda dönüşüm sonrası iptal kontrolü mevcut.
- should-fix: collection child Skip Existing sonucunu Activity uyarısıyla göstermiyor; M6.3 mevcut
  skip/identity davranışını korudu. Activity/SSE/transaction sahipliği ayrı scoped iş gerektirir.
- Sonnet/Opus uygulama/test ve açık aynı-model resume görüldü; farklı-rol resume izolasyonu
  denenmedi. Dosya kapsamı prompt kuralıdır, teknik sandbox değildir. Native bg/attach yok.
- should-fix: WORKFLOW kullanımına, modelsiz implement resume'ın CLI hatası verdiği açıkça eklenebilir;
  aynı modelin açık yeniden seçimi zaten kuraldır. Gözlenen model yalnız init'ten, lead de doğrular.
- should-fix: izleyicinin partial-line testinde buffer adımları daha deterministik doğrulanabilir;
  reviewer uygulamada ilgili bir hata görmedi, otomatik takip için bloklamıyor.

## Son gate

- 2026-10-30 macOS, WF-R1-smoke: yeni GPT reviewer status/diff --check/Read
  başarılı raporladı, must-fix yok; lead status/diff --check tekrarı temiz.
  Exit kodları child araç yanıtında sunulmadı; ham child trace bağımsız incelenemedi.
  Yalnız doğrulama/STATE işi; uygulama testleri ve full gate çalıştırılmadı.
- 2026-10-30 macOS, WF-R1: worker Read kontrolleri ve lead diff incelemesi /
  `git diff --check` PASS; bağımsız GPT must-fix yok. Opus son review wrapper çağrısı
  shell reddiyle başlamadı; kullanıcı yalnız bu işe özel ek Opus review olmadan kapanışı
  onayladı. Lead diff/whitespace kontrolünü kapanışta tekrarladı. Yalnız prose: uygulama testleri ve
  full gate çalıştırılmadı; restart sonrası yeni reviewer davranışı doğrulanmadı.
- 2026-10-30 macOS Python 3.14.9, M6.4d-d3 worker ve lead: hedefli 11 test dosyası
  885 PASS, bir deprecation uyarısı; altı dosyada Ruff/Mypy PASS, lead diff --check PASS.
  Bağımsız GPT review: must-fix yok. Full gate/frontend/gerçek binary smoke çalıştırılmadı.
- 2026-10-30 macOS Python 3.14.9, d2 worker ve lead: single_conversion/conversion_plan/
  prepared_media/ffmpeg/output_profiles/jobs/child runner/dispatch/library_catalog 9 dosya:
  430 PASS, iki deprecation uyarısı. Lead hedefli Ruff/Mypy (6 dosya) PASS; GPT must-fix yok.
  Full gate/frontend/gerçek binary smoke çalıştırılmadı; eski full gate güncel değil.
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
