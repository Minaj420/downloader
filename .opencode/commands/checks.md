---
description: Projenin kalite kapılarını çalıştır
agent: fast-worker
subtask: true
---

Türkçe cevap ver.

AGENTS.md içindeki kalite kurallarını uygula.

Bu görev yalnızca kontrolleri çalıştırmak içindir.
Dosya değiştirme.

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

Docker ile ilgili değişiklik yoksa gereksiz yere image build etme.

Hiçbir problemi gizleme.

Sonuçları PASS / FAIL olarak özetle.

Başarısız bir kontrol varsa:
- hangi komutun başarısız olduğunu,
- kısa hata nedenini,
- bunun commit'i engelleyip engellemediğini

belirt.

Dosya değiştirme.
Stage etme.
Commit veya push yapma.