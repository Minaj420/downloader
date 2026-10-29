---
description: Birincil OpenCode akışı — Opus danışman/uygulayıcı, GPT reviewer, Luna checker
agent: lead
model: openai/gpt-6.1-sol
---

Türkçe cevap ver.

`STATE.md` ve `docs/WORKFLOW.md`'yi oku ve döngüyü uygula. Argüman bir sayıysa
en fazla o kadar iş yap (üst sınır 5); boşsa tek iş. Ek talimat varsa kapsamı
ona göre daralt:

Milestone öncesi Opus görüşünü ve lead kararını görünür özetle; görüş yoksa açık
kullanıcı onayı olmadan uygulama yok. Claude /devam'a yönlendirme. Tek yazıcı,
bir review düzeltme turu, kota/kimlik/izin hatasında tekrar veya sessiz fallback yok.

$ARGUMENTS

Bitince kısa özet ver ve dur.
