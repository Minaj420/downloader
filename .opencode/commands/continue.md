---
description: Yedek akış — sıradaki işi (veya N işi) docs/WORKFLOW.md döngüsüyle yürüt
agent: lead
model: openai/gpt-6.1-sol
---

Türkçe cevap ver.

`STATE.md` ve `docs/WORKFLOW.md`'yi oku ve döngüyü uygula. Argüman bir sayıysa
en fazla o kadar iş yap (üst sınır 5); boşsa tek iş. Ek talimat varsa kapsamı
ona göre daralt:

$ARGUMENTS

Bitince kısa özet ver ve dur.
