---
description: OBSOLETE — GPT uygulayıcı devre dışı; lead Claude Code Opus bridge kullanır
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
İzinli dosyalar ve Opus'un çalıştıracağı hedefli kontroller açık görev metnindedir.
Anthropic API provider/model kurulmaz; Claude Code aboneliği ve `--model opus` kullanılır.
