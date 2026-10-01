---
description: Ana Lead oturumunda sıradaki tek bounded işi ilerlet
agent: lead
model: zai-coding-plan/glm-5.3-flash
---

Türkçe cevap ver.

`lead.md`'de tanımlı `/continue` çalışma döngüsünü şimdi bir kez çalıştır.
`.opencode/WORK_STATE.md` yetkilidir (authoritative); session inference veya
eski checkpoint özetleri onunla çelişirse durable state geçerlidir. Mevcut ana
session bağlamını koru; yeni ve bağımsız bir orchestration session başlatma.

$ARGUMENTS

Bu çağrıda yalnızca bir bounded task veya birbirinden ayrılamayan tek fix
grubu tamamla. İş bittiğinde kısa checkpoint raporu ver ve dur; önerilen
sıradaki işi aynı çağrıda başlatma, kullanıcının yeni `/continue` çağrısını bekle.
