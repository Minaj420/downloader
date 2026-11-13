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
Tek fetch yalnız remote-tracking metaverisini günceller; bu, Git'e mutlak salt-okunurluk iddiası değildir.
Son tazelik sıfırlama tetikleyicisinden sonra geçerli fetch kanıtı yoksa repo kökünü araç `workdir`
parametresiyle vererek ayrı, doğrudan tek `git fetch --verbose origin` çağrısı yap; geçerli kanıt
varsa tekrarlama. Fetch sonrası WORKFLOW'daki status/log/dal kontrollerini ayrı doğrudan
çağrılarla uygula; geçerli fetch kanıtı olsa bile dal kontrolünü atlama. Aynı oturum/worktree/dalda
yalnız lead'in kendi commit'leri istisnası WORKFLOW'daki gibidir. Hata, izin/kimlik/ağ/kota
hatası, sıfır olmayan çıkış, timeout veya etkileşimli istemde dur; aynı veya diğer biçimle tekrar,
fallback, worker devri veya izin aşma yok. Kullanıcı terminal çıktısı yalnız isteğe bağlı kanıttır,
otomatik istenmez. Kullanıcı fark kabulü başarısız fetch'i aşmaz; otomatik pull/merge/push yok.

Rapor: çalışma dalı ve HEAD, çalışma ağacı, upstream ile ileri/geri durumu,
`origin/main` entegrasyon dalına göre durum, doğrulanabilen milestone/alt iş ve sıradaki
adım. Yerel temizliği uzak güncellikle karıştırma; hazır olmayı testlerin geçmiş olması
veya ortam/Claude girişinin doğrulanması olarak sunma.

$ARGUMENTS

DOĞRULANDI yalnız geçerli fetch kanıtı ve engel bulunmayan karşılaştırma içindir; test/gate veya ortam hazır iddiası değildir. Açıkça kabul edilmiş farkı varsa raporda belirt; bu yalnız-rapor komutu bu kabulü geliştirme başlatmak için kullanmaz.

Son satır dört biçimden biri olsun:
- `Oturum: DOĞRULANDI — <dal> <kısa HEAD>`
- `Oturum: DURDU — <somut neden>`
- `Oturum: GÜNCELLİK DOĞRULANMADI — <somut neden>`
- `Oturum: KULLANICI KARARI GEREKLİ — <dal> <HEAD>; fark: <özet>`
