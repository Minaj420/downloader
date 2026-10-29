---
name: devam
description: OBSOLETE — eski Claude merkezli orkestrasyon; yalnız kullanıcı açıkça /devam çağırırsa pasif geçiş açıklaması
disable-model-invocation: true
---

# /devam — pasif

Bu skill uygulama veya orkestrasyon başlatmaz. Tek yönetim OpenCode `lead` ve
`/continue`'dur; kurallar `docs/WORKFLOW.md`'dedir. Kullanıcıya bunu bildir ve dur.
OpenCode subprocess/başka ajan çağırma, recursive delegasyon yapma, dosya/STATE
yazma, test/stage/commit çalıştırma. Opus child çağrılarında skills/commands kapalıdır.
