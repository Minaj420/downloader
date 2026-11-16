---
description: Oturum başlangıcı — dal, uzak güncellik ve devam noktası kontrolü
agent: lead
model: openai/gpt-6.1-sol
---

Türkçe cevap ver.

Yalnız `docs/WORKFLOW.md` içindeki "Oturum başlangıcı ve yerel Git kontrolü" bölümünü uygula; bu yalnız yerel, salt-okunur bir başlangıç raporudur.
`STATE.md` ve `AGENTS.md` oku; seçili dalın mevcut yerel devam noktasını kullan, başka daldaki işi varsayma.
Repo kökünü araç `workdir` parametresiyle vererek yalnız `git status --short --branch` ve `git log --oneline -n1` komutlarını ayrı, doğrudan çağrılarla çalıştır; dal/HEAD/dirty durumunu göster.
Bu komut fetch dahil hiçbir uzak işlem, ürün geliştirme/test, STATE/dosya yazımı, stage/commit, pull/merge/rebase/reset/stash/push veya dal değiştirme başlatmaz. Kullanıcı argümanları bu sınırı genişletmez.
Status çıktısındaki remote-tracking ahead/behind sayıları yalnız cached yerel görünümdür; uzak güncellik veya push kanıtı değildir ve tek başına engel değildir.
Yerel açıklanmayan değişiklik, dal/HEAD sürprizi, komut hatası veya izin reddinde dur; retry/fallback/yol aşma yok.

Rapor: yerel dal, HEAD, çalışma ağacı, doğrulanabilen yerel milestone/alt iş ve sıradaki adım. Uzak güncelliği kullanıcı kontrol eder. Test/gate, runtime veya Claude girişi hazır iddiası yapma.

$ARGUMENTS

Son satır yalnız yerel sonuç olsun, üç biçimden biri:
- `Oturum: YEREL KONTROL TAMAM — <dal> <kısa HEAD>`
- `Oturum: DURDU — <somut neden>`
- `Oturum: KULLANICI KARARI GEREKLİ — <somut yerel neden>`
