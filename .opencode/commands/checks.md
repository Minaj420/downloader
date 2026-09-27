---
description: Projenin kalite kapılarını çalıştır
---

Türkçe cevap ver.

AGENTS.md içindeki kalite kurallarını uygula.

Backend için mevcutsa:
- ruff check .
- mypy app tests
- pytest

Frontend için mevcutsa:
- npm run lint
- npm run typecheck
- npm test
- npm run build

Docker ile ilgili değişiklik varsa:
- docker compose build
- container startup
- /api/v1/health
- SPA erişimi

Dosya değiştirme.
Commit veya push yapma.

Sonuçları PASS / FAIL olarak özetle.
Başarısız kontrollerin kısa nedenini belirt.