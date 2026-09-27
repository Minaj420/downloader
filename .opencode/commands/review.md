---
description: Mevcut değişiklikleri bağımsız reviewer ile incele
agent: reviewer
model: openai/gpt-5.6-sol
subagent: true
---

Türkçe cevap ver.

Önce:
- AGENTS.md
- PRODUCT_SPEC.md
- ARCHITECTURE.md

dosyalarını oku.

Ardından mevcut Git durumunu, tracked diff'leri ve ilgili untracked dosyaları incele.

$ARGUMENTS

Şunlara odaklan:

- correctness
- regression riski
- ürün spesifikasyonuna uyum
- mimari ihlaller
- gereksiz karmaşıklık
- güvenlik sorunları
- eksik veya zayıf testler
- ilerideki milestone'ları zorlaştıracak kararlar

Bulguları şu sırayla ver:

- Critical
- Important
- Minor

Her bulguda:
- dosya veya alan,
- neden problem olduğu,
- önerilen somut düzeltme

belirt.

Sonunda:
- iyi yapılanları,
- commit'e hazır olup olmadığını,
- tekrar çalıştırılması gereken kontrolleri
özetle.

Kod değiştirme.
Dosya stage etme.
Commit veya push yapma.