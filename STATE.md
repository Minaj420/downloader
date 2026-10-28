# Durum

Bu dosya güncel durumun tek özetidir; üstüne yazılır (kurallar: `docs/WORKFLOW.md`).
Son güncelleme: 2026-10-29

## Şu an

- M0–M5 kabul edildi (M5: playlist/album; commit `d37b78f`).
- Workflow Faz 1 tamam: `/devam` (Claude Code) + yedek `/continue` (opencode); canlı izleme `izle`.
- Manuel smoke testi yapıldı (2026-10-29, Fedora + Podman, yt-dlp 2026.10.26, FFmpeg 7.1.5):
  1 binary'ler PASS · 2 tek ses PASS · 3 birleştirme gerektiren video PASS · 4 playlist PASS
  (seçim, sıra, sayaçlar doğru) ama dosya izni hatası doğrulandı. 5–6 (iptal, yeniden başlatma) atlandı.
- M6 — Metadata ve isimlendirme: PLANLAMA. Implementation başlamadı.

## Sıradaki iş

Kural: `/devam` her işi review + kontrollerden sonra otomatik commit eder; push yalnız
kullanıcı "push et" deyince.

1. M6 ürün kararlarını kullanıcıyla netleştirmek (aşağıda); smoke testteki gerçek çıktılar elde.
2. Kararlar sonrası M6'yı küçük işlere bölüp `/devam` ile yürütmek.

## Açık kararlar (kullanıcı verecek)

M6:
- Müzik dosyaları hangi biçimde kaydedilsin? (Smoke: YouTube sesi `.webm`/Opus geldi; Plex ve
  birçok çalar için sorunlu. Seçenekler: `.opus` olarak çıkar, M4A veya MP3'e dönüştür.)
- Sanatçı / albüm / parça bilgisi eksikse klasör ve dosya adı ne olur?
- Playlist parça numarası nereden gelir (kaynak sırası mı, metadata mı)?
- Kaynak başlıklarındaki "(Audio)", "Official Video" gibi ekler temizlensin mi?
- Kapak görseli aday kaynağı ve güven seviyesi; hangi biçimlere metadata/kapak gömülür?
M9 (arayüz):
- Video kalite etiketleri: geniş videolarda "858p, 804p, 536p..." gibi sayılar çıkıyor; bilinen
  basamaklara (1080p/720p/480p) oturtulsun, liste kısalsın, her seçenekte boyut görünsün mü?

Smoke'tan M6 gözlemleri: dosyalar `audio/`·`video/` altına düz konuyor; albüm adı kullanılmıyor;
başlıktaki `:` → `_`; platform alanı boş (YouTube tanınmadı); video `.mkv` (VP9+AAC).
Kaynaklar: PRODUCT_SPEC §13–16, ARCHITECTURE §30–34 ve §59 Milestone 6.

## Açık bulgular

- **must-fix (M7 öncesi):** playlist dosyaları `0600` izinli — smoke'ta gerçek indirmede doğrulandı
  (tek parça/video `0644`). `storage_service.py` child yerleştirme `mkstemp` modunu koruyor.
- should-fix: container normal kapatmada (SIGTERM) 10 sn içinde durmadı, Podman SIGKILL kullandı.
  Kuyruk boştu. Kapanış akışı incelenmeli (orderly shutdown settlement bu yola bağlı).
- should-fix: Python 3.14'te 3 test başarısız (bkz. Son gate).
- Not: Elisa `.m4a` dosyasını dışarıdan açınca çalmadı; VLC ve GStreamer/FFmpeg sorunsuz (oynatıcı sorunu).

## Son gate

- 2026-10-29, macOS: backend ruff/mypy PASS, pytest 1466 PASS; frontend tamamı PASS (147 test).
- 2026-10-29, Fedora (Python 3.14): ruff/mypy PASS; pytest 1462 PASS, 1 skip, **3 FAIL** —
  `test_rejected_output_closes_all_walk_descriptors`: testin `os.close` monkeypatch'i 3.14 asyncio
  pidfd'siyle çakışıyor (test/ortam sorunu; Docker 3.12). Frontend tamamı PASS.

## Bilinen riskler

- Container root çalışıyor; yt-dlp sürümü sabit değil ve hiçbir yerde görünmüyor.
- Dosya yerleştirme hedefte hard link desteği istiyor (bazı NAS/SMB mount'larında sorun olabilir).
- Accepted: Delete File'da aynı dizin stat/unlink mikro-penceresi; bazı terminal parent DTO'larında
  `error.retryable` gösterilemiyor. Ertelenmiş: cancel 404 kodu, `_cancel_events` temizliği.

## Sonraki fazlar

- Faz 2 — teknik: 0600 izni, kapanış süresi, `compose.yml` `user:`, yt-dlp sürüm sabitleme +
  görünürlük, GitHub Actions CI (Python 3.12).
- Faz 3 — ARCHITECTURE.md'yi bölme (referans / kararlar / tarihçe; içerik taşınır, silinmez).
- M6 → M7 (Plex) → M8 (cihaza indirme) → M9 (cilalama) → v0.1 güvenlik denetimi.
