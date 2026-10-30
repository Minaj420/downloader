# Durum

Güncel özet; iş akışı `docs/WORKFLOW.md`. Son güncelleme: 2026-10-30.

## Şu an

- M6 — Metadata ve Adlandırma → M6.4f-f5 — Dönüşüm kapanışı TAMAMLANDI.
  M6.4a–f dönüşüm kapsamı kapandı; M6'nın bütünü ve M6.5+ açık.
- Opus kapanış görüşü alındı (requested Opus / observed claude-opus-5-5, başarı,
  izin reddi yok). Belge must-fix'i `493df18` ile giderildi; full gate aşağıda PASS.
  İlk checker cat izin reddinde durdu, hiçbir gate çalışmadı; kullanıcı açık onayından
  sonra yeni checker Read ile okuyarak tamamladı. İzinler/config değiştirilmedi.
- Dönüşüm: kalıcı çıktı profili, single/collection runner ve API/UI bağlantısı;
  gerçek codec/container, packet copy ve iptal smoke; tekrar iptalde bounded reap;
  MP4A alt türleri ve H.264 profil politikası tamam. Ayrıntılar git geçmişinde.
- Kodlar bağımsız GPT review'dan geçti; açık must-fix yok. Bu kapanış yalnız STATE;
  üretim/test/config değişmedi. M0–M5 ve M6.0–M6.3 daha önce tamamlandı.
- Mod: Normal, lead OpenCode. Claude-only döneminin bağımsız GPT review'u tamamlandı.
- Kalıcı iletişim tercihi: ilerleme ve sıradaki işte milestone numarası/adı,
  alt iş kodu ve kısa açıklamayı belirt. Otomatik commit; push yalnız açık taleple.
- Dört eski untracked `.opencode/archive` dosyası korunur: `_porcelain_probe_empty.md`,
  `_porcelain_probe_long.md`, `_porcelain_probe_target.md`, `_recovery_p_art.md`.

## Sıradaki iş

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

## Son gate

- 2026-10-30 macOS Python 3.14.9, M6.4f-f5 checker: backend Ruff/Mypy PASS
  (91 dosya), pytest 2197 PASS/0 skip; smoke dosyası 23 PASS/0 skip.
- Frontend lint/typecheck PASS; 7 dosyada 163 test PASS; build PASS; kontrast 20 kontrol PASS.
- Lead hedefli adaptör+smoke tekrarı 82 PASS/0 skip; diff --check PASS.
  Full pytest dört deprecation uyarısı (Starlette/httpx ve SQLite datetime); hedefli tekrar bir.
  Önceki hedefli gate ayrıntıları geçmişte; bu gate'de uygulama kaynakları değişmedi.

## Bilinen riskler

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
