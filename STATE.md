# Durum

Güncel özet; iş akışı `docs/WORKFLOW.md`. Son güncelleme: 2026-11-13.

## Şu an

- WF — Komut bakımı bootstrap TAMAMLANDI; tek seferlik istisna TÜKETİLDİ/KAPALI.
  `implement --maintain-commands` yalnız mevcut continue.md/oturumabasla.md gövdelerine
  dar Edit izni verir; Bash/Write/test/resume yok. Rol/resume yanlış kullanımı provider ve
  snapshot öncesi reddedilir. Normal implement/consult/review argv/izin/prompt/env korundu;
  CLI seçenek kısaltmaları artık reddedilir. Opus uyguladı; GPT review ve salt-okunur Opus
  güvenlik incelemesi tamam, tek düzeltme turunda gövde yetki aktarımı checklist'i eklendi.
  Frontmatter/gövde kapsamı prompt ve bağımsız diff review şartıdır, teknik alan-sınırı garantisi değil.
  Sonraki ayrı onaylı komut bakımında iki izinli dosyanın canlı Edit'i başarılı oldu;
  canlı deny/yol dışı reddi sınanmadı. Fake CLI testleri teknik sandbox kanıtı değil.
  lead.md bootstrap bölümü mevcut dosyada yok; kullanıcı sondaki whitespace satırını da kaldırdı.
  scripts/test/WORKFLOW için tek seferlik Opus istisnası tekrar kullanılmaz.
- WF — Dar fetch komut gövdesi uyumu tamamlandı: kullanıcı lead.md'ye yalnız `git fetch origin` ve
  `git fetch --verbose origin` allow ekledi; lead diff'i doğruladı. Sonnet docs/WORKFLOW.md'yi
  lead fetch akışına güncelledi; `.opencode/commands/oturumabasla.md` ve `continue.md` Edit
  çağrıları izin reddi aldı, dosyalar değişmedi. Adapter success=false/permission_denials=true;
  başka worker/model/yolla tamamlama yok, duruldu. Sonraki açık bootstrap ayrı onayla yapıldı;
  ardından ayrı onaylı `--maintain-commands --model opus` çağrısında iki gövde güncellendi.
  Frontmatter ve ARGUMENTS korundu; tek lead fetch, dal kontrolü, hata/ret/no retry sınırları
  WORKFLOW ile uyumlu. GPT review must-fix yok, düzeltme turu gerekmedi.
  Önceki fetch WORKFLOW hunk'ları bootstrap belgesiyle korunarak
  aynı commit kapsamına alındı, GPT/Opus statik incelemesinden geçti. Kullanıcının lead.md fetch
  allow satırları bu kapanışın commit kapsamındadır; yalnız iki tam fetch komutu, wildcard yok.
  Önceki bootstrap çağrısında tek lead fetch başarıyla çalıştı; başlangıç slash kapısı
  yeni gövdeler yüklenince canlı doğrulanacak, şu an doğrulanmış sayılmıyor.
- WF — Oturum başlangıcı: `/oturumabasla` salt-okunur Git/devam noktası raporu eklendi;
  yeni oturum/makine/dal sonrası taze fetch kanıtı olmadan iş seçilmez. `/continue` ve
  doğrudan iş talepleri de aynı WORKFLOW kapısına bağlı. Yeni fetch akışı lead'de;
  kullanıcı çıktısı zorunlu değil, yeni komut gövdeleri restart sonrasında yüklenir.
  Mekanik worker, bağımsız GPT review ve salt-okunur Opus review tamam; tek düzeltme turunda
  tazelik sıfırlaması/upstream STATE ayrımı netleşti. OpenCode restart ve canlı komut denemesi bekliyor.
- M6 — Dönüşüm uzlaştırması: `origin/main` 84014a1 ilerlemiş sürümü esas alındı;
  yerelde tekrarlanan 5 commit `backup/local-conversion-c0263f2` dalında korunuyor.
  Çalışma dalı `reconcile/conversion`; eski `main` hâlâ ayrışmış durumda, toplu merge/rebase/reset yok.
  Yalnız eksik hedef koruması taşındı: merge/convert başlangıçta mevcut hedefi (symlink/dangling/dizin
  dahil) FAILED ile süreç ve temizlik öncesi reddeder; inceleme hataları fail-closed.
  Her merge/convert yeni token'lı staging adı kullanır; önceki çıktılar korunur, retry çakışmaz.
  API/UI, codec/H.264 politikası ve bounded reap değişmedi. Opus görüşü/uygulama ve bağımsız
  GPT review tamam; yeni bulgu yok, düzeltme turu gerekmedi. Push yapılmadı.
- M6 — Metadata ve Adlandırma → M6.4f-f5 — Dönüşüm kapanışı TAMAMLANDI.
  M6.4a–f dönüşüm kapsamı kapandı; M6'nın bütünü ve M6.5+ açık.
- Opus kapanış görüşü alındı (requested Opus / observed claude-opus-5-5, başarı,
  izin reddi yok). Belge must-fix'i `493df18` ile giderildi; full gate aşağıda PASS.
  İlk checker cat izin reddinde durdu, hiçbir gate çalışmadı; kullanıcı açık onayından
  sonra yeni checker Read ile okuyarak tamamladı. İzinler/config değiştirilmedi.
- Dönüşüm: kalıcı çıktı profili, single/collection runner ve API/UI bağlantısı;
  gerçek codec/container, packet copy ve iptal smoke; tekrar iptalde bounded reap;
  MP4A alt türleri ve H.264 profil politikası tamam. Ayrıntılar git geçmişinde.
- Kodlar bağımsız GPT review'dan geçti; açık must-fix yok. Önceki M6.4 kapanışı yalnız STATE idi;
  sonraki uzlaştırma adaptör/hazırlama ve testleri değiştirdi; eski full gate artık güncel değil.
  M0–M5 ve M6.0–M6.3 daha önce tamamlandı.
- Mod: Normal, lead OpenCode. Claude-only döneminin bağımsız GPT review'u tamamlandı.
- Kalıcı iletişim tercihi: ilerleme ve sıradaki işte milestone numarası/adı,
  alt iş kodu ve kısa açıklamayı belirt. Otomatik commit; push yalnız açık taleple.
- Dört eski untracked `.opencode/archive` dosyası korunur: `_porcelain_probe_empty.md`,
  `_porcelain_probe_long.md`, `_porcelain_probe_target.md`, `_recovery_p_art.md`.

## Sıradaki iş

- lead.md kullanıcı temizliği tamam; iki dar fetch allow satırı korunarak STATE ile ayrı
  commit kapsamına alındı. Push/PR bu kapanışın dışında.
- OpenCode'u bir kez yeniden başlatıp `/oturumabasla` ile tek fetch/dal kontrolü/rapor
  davranışını yeni oturumda canlı doğrula; mevcut runtime reload olmuş sayılmaz.
  Eski bootstrap istisnası kullanılmaz; slash kontrolü henüz canlı doğrulanmış değil.
- Önce uzlaştırma dalını kullanıcıyla ana akışa alma/push kararı; eski ayrışmış `main` üzerinden
  geliştirmeye dönme. Yedek dal korunmalı, force push veya geçmiş değişikliği yapılmamalı.
- M6 — Metadata ve Adlandırma → M6.5 — Metadata/etiket pipeline bağlantısı:
  önce Opus görüşüyle küçük kapsama böl; Original Opus remux ve fallback sınırlarını netleştir.
- Sonra M6.6 müzik yolları/katalog → M6.7 kaynak kapak → M6.8 önizleme/düzenleme →
  M6.9 harici kapak arama (haklar koşullu) → M6.10 genel kapanış.
- Staging/yayınlama sınırlarını koru; kaynak alanlarını tahmin etme. Playlist adı
  otomatik albüm değildir; albüm alanı tek başına kapağı güvenilir yapmaz.

## Açık kararlar

- Kaynak kalite tercihi çıktıdan ayrı; retry profili korur, NULL/eksik Original.
  Tek parent profili; Library dönüşümü, dosya yükleme, child başına çıktı kapsam dışı.
- Ses: Original/MP3/WAV/M4A/Opus/FLAC; sabit profiller, slider yok. MP3 V0,
  M4A AAC 256k, Opus 160k, WAV/FLAC 16-bit; MP4 x264 CRF20 medium + AAC 192k.
- ONAYLI H.264 (kullanıcı 1. seçenek): MP4'te idc 44/110/122/244 transcode;
  yaygın/diğer/eksik profil copy, AAC copy; Original değişmez. Runtime probe yok.
- Açık hedef üretilemezse job/child failed; sessiz Original yayını yok. Hazır hedef
  opsiyonel etiket/kapak hatasıyla dönüşüm öncesi biçime geri düşmemeli.
- Videodan ses çıkarma yok; ses doğrudan ses akışından. Skip Existing dönüşümlü
  işte de mevcut kopyayı tutar. Single uyumsuz plan kayıt/yan etkilerden önce reddedilir.
- PLANLI: Original WebM/Opus→.opus kayıpsız remux, AAC/M4A koruma; geçmiş Library'ye dokunma.
  Etiket/kapak/remux hatasında güvenli çıktı + Activity uyarısı; iptal/güvenlik/storage/
  katalog hatası yutulmaz. Gömme biçimleri ve pipeline detayları henüz netleşecek.
- Metadata uydurulmaz: sanatçı/albüm yoksa düz yerleşim, parçada kaynak başlığı;
  özgün playlist sırası korunur, numaralar sıkıştırılmaz, otomatik başlık temizliği yok.
- Kapak önizlenir ve kullanıcı seçer; sessiz harici arama/değiştirme yok. M6.9 MB/CAA
  yalnız aday zinciri: MB noncommercial/API koşulları ve kapak telifi ayrı; preview/cache/
  embed ve açık kaynak dağıtım hakları entegrasyondan önce netleşmeli. API erişimi telif izni değil.
- Çoklu sanatçı, kısmi metadata yerleşimi/disk öneki açık. M9 kalite/boyut sadeleştirme.
  Yetkili kaynaklar PRODUCT_SPEC §13–16, ARCHITECTURE §30–34/§59; açık kaynak dağıtım hedefi.

## Açık bulgular

- should-fix: collection caller-cancel/başarılı convert testi sleep(0) nedeniyle
  deterministik değil; single başarılı convert+iptal ek testi eksik. Üretim hatası bulunmadı.
- should-fix: child hata/iptal staging temizliği yok; dönüşüm disk kullanımını artırabilir.
- should-fix: boş kuyruk container SIGTERM kapanışı 10 sn'yi aştı; Fedora Python 3.14'te
  eski üç backend failure (os.close monkeypatch/pidfd), mevcut macOS gate bunları doğrulamaz.
- should-fix: child Skip Existing Activity uyarısı yok; ayrı lifecycle/transaction kapsamı.
- should-fix: modelsiz implement resume CLI hatasını workflow'da açıklama ve izleyici
  partial-line testi deterministikliği ayrı takip. WF-R1 dar reviewer smoke tamam;
  restart/model reload ve farklı-rol resume izolasyonu kanıtlanmadı.
- should-fix: Claude-only `/devam-claude` skill'inde STATE mod işaretinden önce yeni başlangıç
  kapısına açık yönlendirme ayrı kapsam; bu işte skill/izin dosyaları değiştirilmedi.
- should-fix: WORKFLOW'daki runtime fetch denenmedi ifadesi tarihsel kayıtla uyumsuz;
  lead.md içindeki eski bootstrap referansı ayrı kullanıcı bakımında değerlendirilmeli.
- should-fix: mevcut geniş git diff/log izinleri --output seçeneğini teknik olarak dışlamıyor;
  ayrı kullanıcı config bakımında değerlendirilmeli; lead yalnız izinli salt-okunur biçimleri kullanır.

## Son gate

- 2026-11-13 Fedora, WF dar fetch izin kapanışı: kullanıcı lead.md temizliğini yaptı;
  diff yalnız iki tam fetch allow satırı. Opus görüşü, bağımsız GPT ve salt-okunur Opus
  güvenlik incelemesi must-fix yok; Opus family eşleşti, izin reddi yok.
  GPT hedefli diff --check PASS. Uygulama testleri/full gate çalıştırılmadı;
  yeni oturum slash davranışı ve canlı izin reddi kapsamı doğrulanmadı.
- 2026-11-13 Fedora, WF komut bakımı: Opus requested/observed family eşleşti, başarı ve
  permission_denials=false; canlı init tools yalnız Edit/Glob/Grep/Read, dontAsk, MCP/skills/
  slash boş. İki gövde Edit'i başarılı, frontmatter/ARGUMENTS korunmuş; GPT review ve lead scoped
  diff --check PASS. Tests NOT RUN (bakımda Bash yok; prompt dosyalarına runtime gate yok).
  Canlı deny-path enforcement ve restart sonrası slash davranışı NOT RUN; izolasyon kanıtı değil.
- 2026-11-13 Fedora Python 3.14.10, WF bootstrap: worker/lead
  `.venv/bin/pytest tests/test_claude_bridge.py -q` 73 PASS (1 Starlette uyarısı);
  `.venv/bin/ruff check tests/test_claude_bridge.py ../scripts/claude-agent.py` PASS;
  `.venv/bin/mypy tests/test_claude_bridge.py` PASS. Scoped diff --check PASS.
  GPT bağımsız ve Opus güvenlik son review must-fix yok. Fake testler canlı CLI enforcement
  kanıtı değil; bakım canlı NOT RUN. Full gate/frontend çalıştırılmadı.
- 2026-11-10 Fedora, WF başlangıç komutu: worker/lead diff --check PASS; command frontmatter
  mevcut biçimle statik doğrulandı. GPT ve Opus son review must-fix yok; gerçek slash yükleme/
  davranış testi restart sonrası bekliyor. Uygulama kodu değişmedi, backend/frontend gate çalıştırılmadı.
- 2026-11-10 Fedora Python 3.14.10, uzlaştırma: worker ve lead ffmpeg_adapter/prepared_media/
  single_conversion/collection_conversion/collection_child_runner/ffmpeg_real_smoke 237 PASS +
  4 SKIP (libx264 encoder yok); jobs/collection_retry 378 PASS. Değişen 6 Python dosyasında
  hedefli Ruff/mypy ve diff --check PASS; GPT review temiz. Gerçek smoke kısmen çalıştı,
  atlanan 4 test doğrulanmadı. Full gate/frontend çalıştırılmadı; aşağıdaki macOS full gate tarihsel.
- 2026-10-30 macOS Python 3.14.9, M6.4f-f5 checker: backend Ruff/Mypy PASS
  (91 dosya), pytest 2197 PASS/0 skip; smoke dosyası 23 PASS/0 skip.
- Frontend lint/typecheck PASS; 7 dosyada 163 test PASS; build PASS; kontrast 20 kontrol PASS.
- Lead hedefli adaptör+smoke tekrarı 82 PASS/0 skip; diff --check PASS.
  Full pytest dört deprecation uyarısı (Starlette/httpx ve SQLite datetime); hedefli tekrar bir.
  Önceki hedefli gate ayrıntıları geçmişte; bu gate'de uygulama kaynakları değişmedi.

## Bilinen riskler

- Accepted (uzlaştırma): private tek yazıcılı staging'de lstat ile FFmpeg açılışı arasında mikro yarış
  sürer; mutlak race koruması değil. Eski token'lı çıktılar cleanup'a kadar ek disk kullanabilir.
- Accepted: f2 kill sonrası 5 sn reap + en fazla 1 sn waiter drain; D-state için mutlak
  reap garantisi yok. Ek iptaller özgün kesintiyi değiştirmez; process-group kapsam dışı.
- Accepted: eski snapshot AAC/profil bilgisi düzeltilmedi; MPEG 69/6b/40.34 layer belirsiz.
  Eksik/bozuk/ondalık veya diğer H.264 profilleri/interlaced copy uyumsuzluğu kalabilir;
  kaynak etiketi doğrulanmaz, her cihazda oynatma garantisi yok. Timeout sabit 3600 sn.
- Accepted: bozulmuş bilinmeyen output profili DTO/SSE'yi bozabilir; sessiz Original fallback yok.
- Gerçek smoke adapter/planner düzeyinde; runner/storage fake binary testli. Gerçek ağ ile
  dönüşüm uçtan uca ve restart smoke yok; adaptörün gerçek iptali artık testli.
- M6.2 containment planlama anında; nested yazmadan önce writer kök/parent doğrulaması şart;
  root içi symlink davranışı TOCTOU koruması değil. Container root, yt-dlp pinsiz, hard link gerekli.
- Claude izinleri OS sandbox değil; built-in plugin'ler kalır, canlı read-only rol Read/Grep/Glob.
  Log kaynak metni içerebilir; izleyiciden çıkmak işi durdurmaz, ana çağrı açık kalmalı.
- Accepted: Delete File stat/unlink penceresi; bazı terminal parent retryable alanları;
  ertelenmiş cancel 404/event temizliği; Elisa M4A oynatma sorunu oynatıcıya özgü.

## Sonraki fazlar

- M6 → M7 Plex → M8 cihaza indirme → M9 cilalama → v0.1 denetim.
- Teknik: kapanış süresi, container user, yt-dlp sabitleme/görünürlük, Python 3.12 CI;
  ARCHITECTURE bölme ayrı takip. Tamamlanan iş ayrıntıları git geçmişinde.
