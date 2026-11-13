---
description: Birincil OpenCode akışı — Opus danışman, Sonnet/Opus uygulayıcı, GPT reviewer, Luna checker
agent: lead
model: openai/gpt-6.1-sol
---

Türkçe cevap ver.

Önce `docs/WORKFLOW.md` "Oturum başlangıcı ve git tazeliği" kontrolünü uygula. Son tazelik sıfırlama tetikleyicisinden sonra geçerli fetch kanıtı yoksa repo kökünü araç `workdir` parametresiyle vererek ayrı, doğrudan tek `git fetch --verbose origin` çağrısı yap; geçerli kanıt varsa tekrarlama. Fetch sonrası WORKFLOW'daki status/log/dal kontrollerini ayrı doğrudan çağrılarla uygula; geçerli fetch kanıtı olsa bile dal kontrolünü atlama. Aynı oturum/worktree/dalda yalnız lead'in kendi commit'leri istisnası WORKFLOW'daki gibidir. Hata, izin/kimlik/ağ/kota hatası, sıfır olmayan çıkış, timeout veya etkileşimli istemde iş seçmeden dur; aynı veya diğer biçimle tekrar, fallback veya worker devri yok. Kullanıcı terminal çıktısı yalnız isteğe bağlı kanıttır, otomatik istenmez. Farklar için WORKFLOW’daki açık kullanıcı kabulü uygulanır; bu kabul fetch başarısızlığını aşmaz. Otomatik pull/merge/push yok. `/oturumabasla` komutunun atlanması kontrolü atlatmaz.

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
