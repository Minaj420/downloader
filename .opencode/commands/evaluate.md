---
description: Task-run ve milestone verisini salt-okunur evaluator ile analiz et
agent: evaluator
model: openai/gpt-5.6-sol
subtask: true
---

Türkçe cevap ver.

Bu komut yalnızca salt-okunur analiz içindir; hiçbir config, model mapping,
routing, WORK_STATE veya production dosya değişikliği yapmaz ve otomatik
uygulama yapmaz.

Önce `.opencode/evals/SCHEMA.md` şemasını oku. Ardından
`.opencode/evals/task-runs.jsonl` kayıtlarını, `.opencode/evals/reports/`
altındaki raporları, `.opencode/WORK_STATE.md` durable state'i ve izinli
salt-okunur git komutlarıyla erişilebilen kanıtı incele.

$ARGUMENTS

Analiz kapsamını argümanlardan belirle (tek task-run, tek milestone veya tüm
veri). Belirtilmemişse milestone bazlı rapor taslağı üret ve raporu SCHEMA.md'deki
sabit bölümlerle yaz.

Kurallar:

- Önce correction/supersession kurallarıyla effective dataset'i kur; superseded
  kayıtları metriklerden çıkar ve correction kayıtlarını implementation rework
  sayma.
- Metrikleri yalnız SCHEMA.md'deki deterministik türetim kurallarıyla hesapla.
  Ana metrikler (first-pass acceptance rate, rework per accepted task,
  time-to-accepted, implementation failure rate, late finding rate, reopened
  finding rate, reviewer overturn rate, checker escape rate, scope
  compliance, provider failure rate) SCHEMA.md §4 formülleriyle hesaplanır;
  her metrik value + eligible n + limitation ile sunulur ve `null` kayıtlar
  paydaya girmez.
- `first_pass_accepted` için milestone kapanış PASS tek başına task-level
  ilk-geçiş kanıtı değildir (SCHEMA.md §4).
- Bulgu keşif aşaması değerleri: `implementation` | `task_review` |
  `milestone_review` | `checker` veya null; checker aşaması olayları review
  bulgusu değildir.
- Bilinmeyen değerleri `unknown`/`not captured` olarak raporla; tahmin etme.
- Kanıtsız sayısal iddia yapma; her sayıya kanıt kaynağı ekle.
- Retrospektif rapor/task-run kendi kendisinin kanıtı değildir; bu iddiaları
  yalnız durable WORK_STATE ve izinli Git kanıtıyla doğrula.
- Provider/quota/auth/availability olaylarını implementation failure'dan ayır.
- Model/role kıyası yalnız risk/difficulty stratification ile yapılır.
- Çift sayım engelleme ve metodoloji raporda açıkça belirtilir.
- Milestone raporlarında SCHEMA.md'deki sabit dokuz bölüm ve Milestone summary
  altında ana metrikler tablosu kullanılır.
- Rapor taslağını yanıt metni olarak döndür. Raporun dosyaya yazılması
  gerekiyorsa bunu evaluator yapmaz; Lead'in mekanik worker'a (fast-worker)
  açıkça delege etmesi gerekir.
- Routing/model önerileri provisional'dır; uygulanmaları kullanıcının açık
  onayına bağlıdır.
