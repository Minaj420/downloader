---
name: normal-mod
description: Claude-only modundan normal OpenCode moduna döner (.claude/settings.json'ı normal mod dosyasıyla değiştirir); yalnız kullanıcı /normal-mod yazarsa
disable-model-invocation: true
---

# /normal-mod

Kullanıcı OpenCode/GPT akışına dönüyor. Kurallar `docs/WORKFLOW.md` "Geçici Claude-only modu" bölümündedir.

1. `bash scripts/mode.sh normal` çalıştır; çıktıyı olduğu gibi bildir.
2. Hata verirse dur, nedeni bildir; dosyaya elle dokunma.
3. Başarılıysa kullanıcıya hatırlat: ilk OpenCode `/continue`, WORKFLOW'un dönüş
   kurallarıyla STATE'teki GPT review başlangıç sınırını ve bu sınırdan sonraki
   bütün commit'leri inceler. Başlangıç mevcut HEAD'in atası değilse veya aralıkta
   merge/pull ya da açıklanamayan geçmiş varsa durulur; bekleyen işaret korunur.
   İşaret ancak bağımsız GPT review ve gerekli düzeltmeler tamamlanınca kaldırılır.
   STATE.md artık korumalıdır; bu skill ona dokunmaz.
4. Başka dosya düzenleme, commit veya iş başlatma.
