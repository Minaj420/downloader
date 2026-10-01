---
description: Projenin kalite kapılarını salt-okunur checker ile çalıştır
agent: checker
subtask: true
---

Türkçe cevap ver.

`checker.md` agent kurallarına göre projenin kalite kapılarını salt-okunur
olarak çalıştır: exact komut listesi, öncesi/sonrası kaynak-değişim kontrolü ve
Docker ortamını başladığı duruma döndürme kuralları checker.md'de tanımlıdır.

$ARGUMENTS

Hiçbir problemi gizleme. Sonuçları PASS/FAIL olarak özetle; başarısız bir
kontrol varsa komutu, kısa hata nedenini ve commit'i engelleyip engellemediğini
belirt.
