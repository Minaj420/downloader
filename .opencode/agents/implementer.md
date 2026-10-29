---
description: OBSOLETE — GPT uygulayıcı devre dışı; lead Claude Code Sonnet/Opus bridge kullanır
mode: subagent
model: openai/gpt-6.1-sol
disable: true
permission:
  edit: deny
  bash: deny
  task: deny
---

Bu ajan aktif uygulayıcı değildir. Lead `docs/WORKFLOW.md` uyarınca
`bash scripts/claude-agent.sh implement` çağrısına scoped görevi düz metin prompt/stdin verir.
İzinli dosyalar ve seçilen Claude modelinin çalıştıracağı hedefli kontroller açık görev metnindedir.
Varsayılan Sonnet sınırları net olağan muhakeme içindir; lead zor/belirsiz veya yüksek
etkili migration, transaction/concurrency/ownership, destructive filesystem/path
güvenliği/veri kaybı ve anlamlı API sözleşmesi/mimari değişikliklerinde baştan açık
`--model opus` seçer. Boyut/dosya sayısı kriter değildir. Aynı adapter ve izinler
kullanılır; ek ajan/runner veya Anthropic API provider/model kurulmaz.
Bu dosya pasif yönlendirme olarak kalır; eski GPT implementer aktif değildir.
