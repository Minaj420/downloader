---
description: Mevcut değişiklikleri bağımsız reviewer ile incele
agent: reviewer
model: openai/gpt-5.6-sol
subagent: true
---

Türkçe cevap ver.

`reviewer.md` agent kurallarına göre mevcut Git durumunu ve ilgili
değişiklikleri bağımsız ve salt-okunur olarak incele.

$ARGUMENTS

Bulguları Critical/Important olarak sınıflandır; her bulguda dosya veya alan,
problemin nedeni ve önerilen somut düzeltme belirt. Sonunda iyi yapılanları,
commit'e hazır olup olmadığını ve tekrar çalıştırılması gereken kontrolleri
özetle.
