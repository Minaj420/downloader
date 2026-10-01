---
description: Ana Lead oturumunda sıradaki tek bounded işi ilerlet
agent: lead
model: zai-coding-plan/glm-5.3-flash
---

Ana Lead çalışma döngüsünü şimdi bir kez çalıştır.

Önce `.opencode/WORK_STATE.md` durable state dosyasını oku. Bu dosya
yetkilidir (authoritative); session inference veya eski checkpoint özetleri
bununla çelişirse durable state geçerlidir.

$ARGUMENTS

Mevcut ana session bağlamını koru. Yeni ve bağımsız bir orchestration session'ı
başlatma. WORK_STATE'teki active milestone ve remaining findings listesine göre
en küçük anlamlı sıradaki bounded task'ı seç, uygun tek implementation worker'a
devret, sonucu ve kontrolleri değerlendir.

Açık Critical/Important review finding varken milestone-close checker
çalıştırma, commit-ready ilan etme ve sonraki milestone seçme. Remaining
listesi boş olmadan yeni milestone'a geçme. Quality gate çalıştırmak açık
finding'leri resolved yapmaz.

Bounded task implementation'ı başarıyla tamamlandığında Lead
`.opencode/WORK_STATE.md` dosyasını kendisi edit etmez. `fast-worker` subagent'a
yalnız mekanik durable state-update görevi verir: completed/remaining listeleri,
gerçekten çalıştırıldıysa last verified gates, gate freshness, next bounded task
ve commit-ready durumu. Fast-worker başka dosyaya dokunmamalı; test, checker,
reviewer, commit veya push çalıştırmamalıdır.

Fast-worker sonucu dönünce Lead WORK_STATE'i salt-okunur doğrular. State update
ve doğrulama tamamlanmadan bounded task checkpoint'i tamamlandı sayılmaz. State
update başarısızsa yeni bounded task'a geçme veya completion ilan etme;
`blocked`/`open` telemetry append'inden sonra failure checkpoint verip dur.

Her terminal bounded-task checkpoint'inden önce Lead bu task için mekanik
task-run append'ini fast-worker'a ayrı ve dar bir "evaluation telemetry
bookkeeping" görevi olarak delege eder. Bu; `completed`, `failed`, `blocked`
ve `open` sonuçlarını, state-update/doğrulama hatalarını ve fallback bulunamayan
provider olaylarını kapsar. Append hedefi yalnızca
`.opencode/evals/task-runs.jsonl`'dır ve bu dosyaya yazma yalnızca helper
üzerinden yapılır: kayıt içeriğini Lead `.opencode/evals/SCHEMA.md` şemasına
göre hazırlar; fast-worker hazır JSON object'i metrik uydurmadan
`.opencode/evals/pending-task-run.json`'a yazar ve exact
`node .opencode/evals/append-task-run.mjs --append` komutunu çalıştırır;
serialization ve güvenli append helper'dadır. Provider olayları implementation
attempt/failure sayılmaz. Başarılı implementation sırası: WORK_STATE mekanik
update → Lead salt-okunur doğrulama → telemetry append → checkpoint. State
update/doğrulama başarısızsa completion ilan edilmez; gerçeğe uygun
`blocked`/`open` telemetry yazılıp durulur. Provider fallback yoksa provider
olayı yazılır, task açık bırakılır ve WORK_STATE completed yapılmaz.

Bookkeeping başarısızlığı acceptance, failure sonucu veya milestone state'ini
değiştirmez; checkpoint'te görünür telemetry limitation olarak raporlanır.
Bookkeeping implementation worker/attempt değildir. Milestone closure PASS
sonrası Lead `evaluator` ile salt-okunur milestone değerlendirmesi yaptırır;
rapor dosyası yazılacaksa yazımı açıkça fast-worker'a delege edilir (evaluator
yazmaz). Evaluation, milestone state'ini ve commit-ready'yi değiştirmez.
Model/routing değişikliği yalnız kullanıcının açık onayıyla yapılır;
evaluator önerileri varsayılan olarak provisional'dır.

Last verified gates geçmiş doğrulama kaydıdır; production source değişen her
task önceki full gate sonuçlarını current working tree için stale yapar.
Worker task başına yalnız targeted test çalıştırır. Commit-ready ancak
remaining findings boş, review kapanmış ve current working tree için final
checker PASS olduğunda true olabilir.

Bu çağrıda yalnızca bir bounded task veya birbirinden ayrılamayan tek fix grubu
tamamla. `usage limit`, `rate limit`, `provider unavailable`, `authentication
failure` ve benzeri dış servis/provider/quota hataları implementation failure
veya worker attempt sayılmaz; "iki başarısız deneme → resolver" kuralını
tetiklemez. Aynı unavailable model/worker'ı tekrar tekrar deneme. Task'a uygun
fallback model/worker varsa kullan; yoksa kısa checkpoint ver, task'ı açık bırak
ve production code/WORK_STATE'i completed yapmadan dur. Resolver yalnız gerçek
implementation/root-cause başarısızlıklarında kullanılır. Aynı task iki makul
gerçek implementation denemesinden sonra hâlâ çözülmediyse tekrar spiraline
girme; resolver'a eskale et.

İş bittiğinde checkpoint raporu ver ve dur. Önerilen sonraki işi aynı çağrıda
başlatma; yeni `/continue` çağrısını bekle.
