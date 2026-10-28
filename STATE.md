# Durum

Bu dosya güncel durumun tek özetidir; üstüne yazılır (kurallar: `docs/WORKFLOW.md`).
Son güncelleme: 2026-10-29

## Şu an

- M0–M5 kabul edildi (M5: playlist/album; commit `d37b78f`).
- Workflow Faz 1 tamam: Z.ai kaldırıldı; `/devam` (Claude Code) + yedek
  `/continue` (opencode). opencode ajan izinleri artık gerçekten uygulanıyor (eski `permissions:`
  listesi opencode tarafından yok sayılıyordu). Çapraz review (gpt-5.6-sol) bulguları giderildi.
- M6 — Metadata ve isimlendirme: PLANLAMA. Implementation başlamadı.

## Sıradaki iş

Kural: `/devam` her işi review + kontrollerden sonra otomatik commit eder; push yalnız
kullanıcı "push et" deyince.

1. İlk `/devam` denemesi (mekanik, zararsız): `backend/app/services/collection_parent_runner.py`
   modül docstring'i bayat ("production dispatch remains guarded", "No retry/reset/recovery");
   dispatch, retry ve recovery artık var. Yalnız docstring düzeltilir.

## Açık kararlar (M6, kullanıcı verecek)

- Sanatçı / albüm / parça bilgisi eksikse klasör ve dosya adı ne olur?
- Playlist parça numarası nereden gelir (kaynak sırası mı, metadata mı)?
- Kapak görseli aday kaynağı ve güven seviyesi nasıl belirlenir?
- Hangi container'lara metadata/kapak gömülür; gömme başarısız olursa ne olur?

M6 için ilk keşif: `StorageService` dosyaları `audio/` ve `video/` altına düz koyuyor;
analiz sonucu müzik metadata/güven bilgisi taşımıyor; FFmpeg yalnız stream-copy merge yapıyor.
Kaynaklar: PRODUCT_SPEC §13–16, ARCHITECTURE §30–34 ve §59 Milestone 6.

## Açık bulgular

- should-fix: Python 3.14'te 3 test başarısız (bkz. Son gate). Faz 2'de CI 3.12'ye sabitlenirken
  ya test 3.14 uyumlu yapılır ya da yerel geliştirme 3.12'ye alınır.

## Son gate

- 2026-10-29, macOS: backend ruff/mypy PASS, pytest 1466 PASS (4 deprecation uyarısı);
  frontend lint/typecheck PASS, 147 test PASS, build PASS, contrast PASS.
- 2026-10-29, Fedora (Python 3.14, checker/gpt-6-luna): ruff/mypy PASS; pytest 1462 PASS,
  1 skip, **3 FAIL** — `test_rejected_output_closes_all_walk_descriptors` (missing/fifo/symlink-parent):
  testin `os.close` monkeypatch'i Python 3.14 asyncio'nun açtığı pidfd ile çakışıyor (test/ortam
  sorunu, uygulama hatası değil; Docker 3.12). Frontend tamamı PASS (147 test). Docker yerine Podman.
- Gerçek ağ / yt-dlp smoke testi hiç çalıştırılmadı (README'deki manuel liste).

## Bilinen riskler

- **Playlist dosyaları `0600` izinli** (`storage_service.py`, `mkstemp`): Plex başka kullanıcıyla
  çalışıyorsa okuyamaz. M7 öncesi düzeltilmeli.
- Container root çalışıyor; yt-dlp sürümü sabit değil ve hiçbir yerde görünmüyor.
- Dosya yerleştirme hedefte hard link desteği istiyor (bazı NAS/SMB mount'larında sorun olabilir).
- Accepted: aynı dizinde stat/unlink mikro-penceresi (Delete File); bazı terminal parent
  DTO'larında `error.retryable` gösterilemiyor.
- Ertelenmiş küçük işler: cancel 404 kodu, `_cancel_events` temizliği, generic 422'de `analysis_id: null`.

## Sonraki fazlar

- Faz 2 — teknik: 0600 izni, `compose.yml` `user:`, yt-dlp sürüm sabitleme + görünürlük, GitHub Actions CI.
- Faz 3 — ARCHITECTURE.md'yi bölme (referans / kararlar / tarihçe; içerik taşınır, silinmez).
- M6 → M7 (Plex) → M8 (cihaza indirme) → M9 (cilalama) → v0.1 güvenlik denetimi.
