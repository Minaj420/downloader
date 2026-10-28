---
description: Task-run ve milestone verisini salt-okunur evaluator ile analiz et
agent: evaluator
model: openai/gpt-5.6-sol
subagent: true
---

Türkçe cevap ver.

`evaluator.md` agent kurallarına göre salt-okunur analiz yap;
`.opencode/evals/SCHEMA.md`'yi authoritative kabul et. Analiz kapsamını
argümanlardan belirle (tek task-run, tek milestone veya tüm veri);
belirtilmemişse milestone bazlı rapor taslağı üret.

$ARGUMENTS

Hiçbir config, model mapping, routing, WORK_STATE veya production dosya
değişikliği yapma ve otomatik uygulama yapma. Raporu yanıt metni olarak döndür;
routing/model önerileri provisional'dır ve yalnız kullanıcının açık onayıyla
uygulanabilir.
