---
name: claude-only
description: GPT kotası bittiğinde Claude-only moduna geçer (.claude/settings.json'ı mod dosyasıyla değiştirir); yalnız kullanıcı /claude-only yazarsa
disable-model-invocation: true
---

# /claude-only

Kullanıcı Claude-only moduna geçmek istiyor. Kurallar `docs/WORKFLOW.md` "Geçici Claude-only modu" bölümündedir.

1. `bash scripts/mode.sh claude-only` çalıştır; çıktıyı olduğu gibi bildir.
2. Hata verirse (ör. settings.json mod dosyasıyla eşleşmiyor) dur, nedeni bildir; dosyaya elle dokunma.
3. Başarılıysa kullanıcıya şunu söyle: izinler yenilenmediyse oturum yeniden başlatılmalı; ardından
   `/devam-claude` ile iş akışı yürütülür; STATE.md'ye mod işaretini lead (devam-claude) yazar.
4. Başka dosya düzenleme, commit veya iş başlatma.
