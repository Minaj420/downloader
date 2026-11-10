---
description: Oturum başlangıcı — dal, uzak güncellik ve devam noktası kontrolü
agent: lead
model: openai/gpt-6.1-sol
---

Türkçe cevap ver.

Yalnız `docs/WORKFLOW.md` içindeki "Oturum başlangıcı ve git tazeliği" kontrolünü uygula.
`STATE.md` ve `AGENTS.md` oku; STATE henüz doğrulanmış devam noktası sayılmaz.
Bu komut geliştirme, test, STATE/dosya yazımı, stage/commit, pull/merge/rebase/reset/
stash/push veya branch değiştirme başlatmaz. Kullanıcı argümanları bu sınırı genişletmez.
Fetch izni yokken komutu deneme veya izinleri değiştirme; son tazelik sıfırlamasından sonra
bu oturuma ait başarılı fetch kanıtı yoksa kullanıcıdan terminal çıktısı iste ve dur.
İzin/kimlik/ağ hatasını başarı sayma.

Rapor: çalışma dalı ve HEAD, çalışma ağacı, upstream ile ileri/geri durumu,
`origin/main` entegrasyon dalına göre durum, doğrulanabilen milestone/alt iş ve sıradaki
adım. Yerel temizliği uzak güncellikle karıştırma; hazır olmayı testlerin geçmiş olması
veya ortam/Claude girişinin doğrulanması olarak sunma.

$ARGUMENTS

DOĞRULANDI yalnız geçerli fetch kanıtı ve engel bulunmayan karşılaştırma içindir; açıkça kabul edilmiş farkı varsa raporda belirt, salt-okunur komut bu kabulü geliştirme başlatmak için kullanmaz.

Son satır dört biçimden biri olsun:
- `Oturum: DOĞRULANDI — <dal> <kısa HEAD>`
- `Oturum: DURDU — <somut neden>`
- `Oturum: GÜNCELLİK DOĞRULANMADI — başarılı fetch çıktısı bekleniyor`
- `Oturum: KULLANICI KARARI GEREKLİ — <dal> <HEAD>; fark: <özet>`
