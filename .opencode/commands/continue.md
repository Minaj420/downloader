---
description: Birincil OpenCode akışı — Opus danışman, Sonnet/Opus uygulayıcı, GPT reviewer, Luna checker
agent: lead
---

Türkçe cevap ver.

Önce `docs/WORKFLOW.md` "Oturum başlangıcı ve yerel Git kontrolü" bölümünü uygula. Repo kökünü araç `workdir` parametresiyle vererek `git status --short --branch` ve `git log --oneline -n1` komutlarını ayrı, doğrudan çağrılarla çalıştır. Açıklanmayan değişiklik, detached HEAD, beklenmedik dal veya açıklanamayan HEAD'de iş seçmeden dur; göreve ilişkin kullanıcı değişiklikleri korunur. Uzak senkronizasyonu kullanıcı yönetir: fetch yapma, fetch çıktısı/kanıt/onay isteme; cached ahead/behind veya upstream yokluğu tek başına engel değildir. Yerel komut hata veya izin reddinde dur; retry, fallback veya yol aşma yok. Otomatik pull/merge/rebase/reset/stash/push/dal değiştirme yok. `/oturumabasla` komutunun atlanması yerel kontrolü atlatmaz.

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
