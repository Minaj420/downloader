---
description: Mevcut değişiklikleri bağımsız reviewer ile incele
agent: reviewer
model: openai/gpt-5.6-sol
subtask: true
---

Türkçe cevap ver.

Önce AGENTS.md, PRODUCT_SPEC.md ve ARCHITECTURE.md dosyalarını oku.

Mevcut Git durumunu ve ilgili değişiklikleri incele.

$ARGUMENTS

Review sırasında:
- kod değiştirme,
- dosya oluşturma veya silme,
- patch uygulama,
- stage etme,
- commit etme,
- push yapma.

Bulguları Critical, Important ve Minor olarak sınıflandır.

Her bulguda:
- dosya veya alan,
- problemin nedeni,
- önerilen somut düzeltme

belirt.

Sonunda:
- iyi yapılanları,
- commit'e hazır olup olmadığını,
- tekrar çalıştırılması gereken kontrolleri
özetle.