---
description: Onaylı kapsamda en fazla üç bounded task'lık kontrollü autopilot yürüt
agent: lead
model: openai/gpt-6.1-sol
subagent: false
---

Türkçe cevap ver.

`lead.md`'de tanımlı `/autopilot` çalışma sözleşmesini bu çağrıda uygula.
`.opencode/WORK_STATE.md` yetkilidir (authoritative); session inference veya
eski checkpoint özetleri onunla çelişirse durable state geçerlidir. Mevcut
ana Lead session bağlamını koru; yeni ve bağımsız bir orchestration session
başlatma. Bu komut bir prompt workflow'dur; deterministic runner, cost timer
veya runtime enforcement değildir ve üst-seviye talimatlarla `lead.md`'deki
izin, routing, failure, quality ve Git guard hükümlerini override etmez.

$ARGUMENTS

Komut argümanları yalnızca kullanıcı onaylı tek kapsam ve bounded görev kuyruğunu
belirtmek içindir. Onaylı kapsam/kuyruğu yoksa görev uydurma; kullanıcıya soru
sor veya kısa checkpoint ile dur. En fazla üç bounded task veya ayrılamayan
fix grubu; task'lar seri yürütülür, ilk seçimden ve her task geçişinden önce
canonical WORK_STATE ve Git durumu yeniden okunur, gereken review kapanmadan
sonraki task'a geçilmez. Üç görev veya kapsam tamamlanınca kısa final özet ver
ve dur; commit/push, yeni milestone, otomatik evaluator ve audit yok. Yeniden
çağrı üç-görev bütçesini otomatik reset etmez; fresh run explicit yeni
kullanıcı onayı ister.
