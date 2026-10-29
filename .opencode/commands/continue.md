---
description: Birincil OpenCode akışı — Opus danışman, Sonnet/Opus uygulayıcı, GPT reviewer, Luna checker
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
Her iş başında “Uygulayıcı: Sonnet/Opus; gerekçe...” de. Varsayılan Sonnet;
WORKFLOW'daki zor/belirsiz/yüksek etkili işlerde baştan açık `--model opus` seç;
boyut/dosya sayısıyla seçme, kör Sonnet→Opus zinciri kurma. Luna yalnız mekanik
düşük risk; belirsiz ürün davranışında kullanıcı kararı şarttır. GPT reviewer her iki
implementer'ı bağımsız inceler; danışman ve mevcut review wrapper sabit Opus kalır.
Resume'da ilk `requested_model` değerini aynı `--model` ile açıkça yeniden ilet;
gözlenen family eşleşmesini doğrula. Model değişimi review rework sınırını sıfırlamaz.

$ARGUMENTS

Bitince kısa özet ver ve dur.
