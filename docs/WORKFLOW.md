# Geliştirme workflow'u

Tek yönetim OpenCode `lead` ve `/continue`'dur. Claude Code `/devam` pasiftir;
Claude child başka ajan/CLI çağırmaz. Kurallar AGENTS.md, durum STATE.md,
ürün PRODUCT_SPEC.md, mimari ARCHITECTURE.md'dir.

## Roller ve modeller

| Rol | Model / çalıştırma | Görev |
| --- | --- | --- |
| lead | OpenCode `openai/gpt-6.1-sol` | Plan, delegasyon, kullanıcı kararları, STATE, commit |
| danışman | Claude Code abonelik oturumu, açık `--model opus` | Salt-okunur görüş |
| implementer | Claude Code abonelik oturumu; varsayılan Sonnet, uzman işte açık `--model opus` | Scoped uygulama ve kendi hedefli test döngüsü |
| reviewer | OpenCode `openai/gpt-6.1-sol` | Bağımsız salt-okunur review; Sonnet ve Opus kodunu inceler |
| fast-worker | OpenCode `openai/gpt-6-luna` | Yalnız mekanik/düşük riskli işler |
| checker | OpenCode `openai/gpt-6-luna` | İstenen gate'ler, kaynak düzenlemez |

Eski GPT `implementer` devre dışıdır. Anthropic API provider kurulmaz.
Config değişiklikleri için OpenCode yeniden başlatılır; mevcut reviewer runtime
modeli reload olmadan değişmiş sayılmaz.

## Döngü

1. STATE ve `git status` oku; açıklanmayan değişiklikte dur, başkasının işine dokunma.
2. **Her milestone öncesi Opus görüşü zorunlu.** Alınamazsa açık kullanıcı onayı
   olmadan uygulama yok. Önemli mimari kararlar önce Opus'a, ürün tercihleri
   kullanıcıya gider. Lead burada **Opus önerisi / gerekçe / alternatif-risk +
   lead kararı** özetini kullanıcıya görünür verir.
3. Tek bounded iş tanımı: amaç, okunacaklar, izinli dosyalar, kabul kriterleri,
   yapılmayacaklar, exact hedefli kontrol komutları ve çalışma dizinleri.
   Belgelenmemiş/çelişkili ürün gereksiniminde dur; soru ve en basit seçeneği raporla.
4. Lead her iş başında **“Uygulayıcı: Sonnet/Opus; gerekçe...”** der. Varsayılan
   uygulayıcı aynı ince adapter üzerinden Sonnet'tir: sınırları net olağan muhakeme.
   Zor/belirsiz işler, high-impact migration, transaction/concurrency/ownership,
   destructive filesystem/path güvenliği/veri kaybı veya anlamlı API sözleşmesi/mimari
   değişikliklerinde baştan açık `--model opus` seçilir. Boyut/dosya sayısı kriter değildir.
   “Sonnet dene, başarısızsa kör Opus” zinciri yoktur; belirsiz ürün davranışı kullanıcı
   kararı gerektirir. fast-worker yalnız mevcut deseni birebir izleyen, net, geri
   alınabilir, hedefli testli mekanik/düşük riskli işlerde.
   Migration/kalıcı veri, transaction/concurrency/ownership, filesystem mutation,
   silme/veri kaybı, güvenlik/path-containment veya API/data contract varsa Luna durur.
   **Tek yazıcıyı lead yönetir; adapter kilit tutmaz.** Worker STATE yazmaz, git yazmaz.
5. Lead diff ve testleri okur; GPT reviewer Sonnet ve Opus kodunu bağımsız inceler. Opus kendi
   koduna rutin reviewer değildir. GPT'nin riskli işlerinde (bu altyapı dahil)
   worker sonrası lead `scripts/claude-review.sh '<odak>'` ile Opus review başlatır.
   Danışman ve mevcut review wrapper hep Opus'tur; genel Sonnet review rolü yoktur.
6. Bulgular: must-fix (doğruluk/veri/güvenlik/spec/önemli test eksiği), should-fix
   (bloklamayan gerçek sorun), accepted-risk (düşük olasılık/etki), over-engineering
   (gereksiz karmaşıklık). Reviewer yeni ürün kararı üretmez.
   **En fazla bir review düzeltme turu**, mümkünse aynı Claude session; lead bu
   politikayı yönetir, adapter tur saymaz. Must-fix sürerse dur.
7. Lead yalnız hedefli kontrolleri kendi yeniden çalıştırır; worker PASS raporu yetmez.
8. Lead STATE'i günceller; review ve kontrollerden geçen her işi otomatik commit eder:
   yalnız görev dosyaları ve STATE tek tek stage, tek davranış commit'i. Göreve özel
   açık git yasağına uyulur. Worker stage/commit yapmaz. Push yalnız açık kullanıcı talebiyle.

Kota/provider/kimlik hatasında tekrar veya sessiz model fallback yok. İzin reddini
başka rol/yolla aşma. Kapsam dışı ihtiyaç, beklenmeyen git değişikliği, etkileşimli
onay ve çözülemeyen must-fix durma nedenidir.

## İnce adapter kullanımı

Repo kökünde:

```text
bash scripts/claude-agent.sh consult 'Öneri, gerekçe, alternatif-risk ...' --log
scripts/claude-review.sh 'Çalışma ağacındaki altyapı değişikliklerini incele' --log
bash scripts/claude-agent.sh implement 'Amaç... İzinli dosyalar... Hedefli kontroller...'
bash scripts/claude-agent.sh implement 'Uzman iş: amaç... İzinli dosyalar... Hedefli kontroller...' --model opus
bash scripts/claude-agent.sh implement 'Tek review düzeltme turu: ...' --resume <claude-session-id> --model sonnet
```

`--model` yalnız `sonnet|opus` kabul eder. `implement` varsayılanı Sonnet'tir;
`consult`/`review` varsayılan ve sabit Opus'tur (`--model opus` kabul edilir,
`--model sonnet` provider başlamadan anlaşılır CLI hatasıdır). Model seçimi tools,
permissions, ortak prompt, test akışı veya environment/OAuth izolasyonunu değiştirmez.
Yeni Python/wrapper/servis ajanı veya runner yoktur.

Örneklerdeki `'<odak>'` gibi metinler öğretici placeholder'dır; ham görev metni
shell komutuna birleştirilmez. Bash araç API'sine giderken prompt tek argüman olacak
şekilde doğru POSIX shell-quote edilir: tek tırnaklı argümandaki gömülü `'` karakteri
`'"'"'` ile escape edilir (örneğin `Opus'un` → `'Opus'"'"'un'`). Eval kullanılmaz.
Wrapper içeride argv/stdin ile aktarır. Prompt argümanı yoksa düz metin stdin'den
okunur; bu yol stdin destekleyen genel API callers içindir. Mevcut Bash aracında
stdin input alanı yoksa düzgün quote edilmiş prompt argümanı kullanılır;
pipe/yönlendirme ile telafi edilmez. JSON görev/check şeması yoktur.
Uzun timeout verir; adapter retry/fallback yapmaz. Session ID stdout sonucundadır.
`--resume` rolü otomatik seçmez: mevcut açık rolün settings/tools izinleri yeniden
verilir; `consult`/`review` için salt-okunur tools/settings tekrar gönderilir.
Bu argv davranışı fake testlerle sınanır; gerçek farklı-rol resume izolasyonu henüz
doğrulanmamıştır. `implement` seçmek lead'in yeni ve açık yazma yetkilendirmesidir;
adapter session kökeni/tur geçmişi tutmaz, uygun aynı-rol resume seçimini lead yapar.
Lead ilk çağrının `requested_model` bilgisine bakar ve resume'da aynı modeli açıkça
`--model sonnet` veya `--model opus` ile yeniden iletir; yukarıdaki resume örneği
Sonnet ile başlayan görev içindir. Sessiz family geçişi yoktur. Aynı model kota
hatasında tekrar yoktur; model değiştirmek tek review düzeltme turu sınırını sıfırlamaz.
Adapter session geçmişi tutmaz; bunlar lead politikasıdır.

`--log` isteğe bağlı tek runtime çıktısıdır: ignored
`tmp/claude/<timestamp>-<pid>.jsonl`. Task/summary deposu, UUID görev kimliği,
active/stale lock ve fix-used kaydı yoktur; eski taslak günlükleri otomatik silinmez.
Başta basılan `WATCH: bash scripts/claude-watch.sh tmp/claude/<dosya>.jsonl` komutu
ikinci terminalde yalnız logu okur; açıklama, tool çağrısı/sonucu ve modelin test
çıktıları görünür, gizli düşünce süreci görünmez. İzleyici Ctrl+C ile kapanınca
Claude'a sinyal göndermez. **Asıl lead çağrısı/terminali açık kalmalıdır.**
Lead'in araç çıktısı bazen çağrı tamamlanınca döner; WATCH satırının kullanıcıya
anında ulaşacağı garanti değildir. Gerekirse kullanıcı logu tmp/claude altında
seçer. Final result yoksa izleyici bekler; Ctrl+C ile çıkılır, adapter eksik finali
başarılı saymaz. İzleyici final result'ta çıkar; process exit doğrulamasının kaynağı
adapter'ın son raporudur. Stderr asıl terminaldedir. Log hassas kaynak metni içerebilir.
Bu yalnız canlı günlük fallback'idir; native `--bg`/attach kullanılmaz veya
doğrulanmış iddia edilmez. Server/tmux/ek altyapı yoktur.

## İzinler ve sınırlar

- `--setting-sources ''` user/project/local settings'i dışlar; inline `--settings`
  rol izinlerini verir. Yalnız `--allowedTools` kullanmak miras izinleri temizlemez.
  HOME/keychain, varsa abonelik OAuth token ve özel `CLAUDE_CONFIG_DIR` korunur;
  API/model/provider ve
  nested-host Claude environment override'ları taşınmaz. `--bare` ve dangerously skip yok.
- `CLAUDE_CODE_SAFE_MODE=1`, bilinen CLI help'teki `--safe-mode` karşılığı olarak
  kullanılır. Explicit settings hooks/auto memory'yi kapatır, enabledPlugins boş
  gönderilir; slash/skills kapalı, strict boş MCP, no-chrome ve dar tools istenir.
  Bu mutlak izolasyon/sandbox veya tüm built-in plugin'lerin kapandığı garantisi değildir.
  Lead'in gerçek read-only review'unda CLI 2.1.312 `system/init` gözlemi:
  tools yalnız Glob/Grep/Read; mcp_servers/skills/slash_commands boş;
  model `claude-opus-5-5`; exit 0, success=true, permission_denials=false.
  Built-in plugin'ler **vardı**: cc-plugin-agents-md, telemetry, plugin-authoring.
  Read-only rolün istenen tools/settings davranışı bu çağrıda görüldü;
  gerçek implementer/test döngüsü ve resume izolasyonu hâlâ doğrulanmadı.
  Managed policy geçerlidir; fake testler canlı izolasyon kanıtı değildir.
- consult/review yalnız Read/Grep/Glob kullanır; Bash/Edit/Write kaldırılmıştır.
  Git status/diff snapshot'ını adapter sabit read-only argv ile prompt'a ekler;
  external diff/textconv kapalıdır. Untracked dosyalar diff'te yoktur, Read ile okunur.
- Her iki implement modeli aynı normal Edit/Write ve yalnız proje test/lint Bash izinlerini alır:
  backend `.venv/bin/pytest`, `.venv/bin/ruff check`, `.venv/bin/mypy`; frontend
  `npm test`, `npm run lint/typecheck/build/check:contrast`. İlgili backend/frontend
  dizinine ayrı `cd` ile geçebilir; zincir/pipe/yönlendirme yoktur. Git yazma,
  destructive shell, başka CLI/ajan komutu allow listesinde değildir; `dontAsk`
  izin isteyen diğer eylemleri reddeder. STATE ve kalıcı ajan config düzenlemesi yasaktır.
- **Tool izinleri OS sandbox değildir.** İzinli dosya listesi lead'in açık prompt
  sınırıdır; dosya bazında teknik enforcement yoktur. Testler/config keyfi kod
  çalıştırabilir ve yan dosya yazabilir; Bash pattern'leri mutlak güvenlik garantisi
  değildir. Hedefli test seçimi ve tek yazıcı lead'in sorumluluğundadır.
- Exit != 0, eksik final, final is_error, permission denial veya bozuk stream
  başarısızdır. Stdout requested/observed model, session, exit, result ve başarıyı
  ve varsa permission_denial_details listesini gösterir; lead gözlenen model family'nin
  seçilen `requested_model` ile eşleştiğini doğrular. Açık karşı family gözlemi
  (ör. Sonnet istenirken `claude-opus-...`) adapter'da başarısızdır; eksik/tanınmayan
  model lead doğrulamasını gerektirir, adapter model registry tutmaz. Adapter aktif retry,
  fallback veya test çalıştırıcısı değildir; CLI iç retry davranışını kontrol etmez.

CLI semantics kaynakları: https://code.claude.com/docs/en/cli-reference ,
https://code.claude.com/docs/en/settings , https://code.claude.com/docs/en/headless .

## Gate ve STATE

Küçük işlerde yalnız görevdeki hedefli kontroller. Milestone kapanışında checker:
backend `.venv/bin/ruff check .`, `.venv/bin/mypy app tests`, `.venv/bin/pytest`;
frontend `npm run lint`, `npm run typecheck`, `npm test`, `npm run build`,
`npm run check:contrast`. Her biri kendi backend/frontend çalışma dizininde.
Sonuç STATE'e tarih + makine ile; kaynak değişince önceki gate bayattır.
STATE üstüne yazılan yaklaşık 60 satırlık durumdur, journal değildir; log/task/session
kimliği ve ham çıktı içermez. Bölümler: Şu an, Sıradaki iş, Açık kararlar, Açık bulgular,
Son gate, Bilinen riskler, Sonraki fazlar. Yalnız lead yazar.
