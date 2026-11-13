# Geliştirme workflow'u

Tek yönetim OpenCode `lead` ve `/continue`'dur. Claude Code `/devam` pasiftir;
Claude child başka ajan/CLI çağırmaz. Kurallar AGENTS.md, durum STATE.md,
ürün PRODUCT_SPEC.md, mimari ARCHITECTURE.md'dir.

GPT kotası bittiğinde yalnız kullanıcının başlattığı geçici istisna:
"Geçici Claude-only modu" (aşağıda).

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

## Oturum başlangıcı ve git tazeliği

Amaç: başka makinede yapılmış işi eski STATE üzerinden tekrarlamamak. Yeni oturum,
makine/worktree/dal değişimi, açıklanamayan HEAD değişikliği, başka yerde push/pull
haberi veya konuşma özetlemesi sonrası kanıt belirsizliği başlangıç kontrolü gerektirir.
`/oturumabasla` yalnız kontrol ve rapordur; `/continue` ve doğrudan geliştirme talepleri
kontrol bu oturumda yapılmadıysa önce aynı kapıdan geçer. Slash komutunu unutmak bypass değildir.
Bu tetikleyicilerde önceki fetch kanıtı ve dal/HEAD kabulü geçersizleşir; yeni kontrol için son tetikleyiciden sonra, doğru repo ve `origin` remote'u için alınmış başarılı bir fetch kanıtı gerekir (lead'in tek fetch çağrısı veya kullanıcının isteğe bağlı paylaştığı başarılı çıktı). Yalnız aynı oturum/worktree/dalda lead’in kendi commit’leri bu sıfırlamanın istisnasıdır.

Önce `git status --short --branch` ve `git log --oneline -n 1` ile dal/HEAD ve dirty durumu
kontrol et. Açıklanmayan değişiklik, detached HEAD veya beklenmedik dalda dur; otomatik
stash/temizleme/dal değiştirme yapma. STATE bu aşamada yalnız bağlamdır, iş listesi değildir.

Uzak tazelik, son tazelik sıfırlama tetikleyicisinden sonra, doğru repo/`origin` remote'u için
başarılı bir fetch kanıtıyla gösterilir. Eski `origin/main`, temiz çalışma ağacı, STATE veya başka
makinedeki kontrol bu kanıt değildir.

Fetch yetkisi yalnız OpenCode `lead`'indedir ve dardır: yalnız tam `git fetch --verbose origin`
(tercih edilen) ve izinli eşdeğeri `git fetch origin`. Lead repo kökünü araç `workdir`
parametresiyle verir ve komutu ayrı, doğrudan tek çağrı olarak çalıştırır; aynı kontrolde iki
biçim peş peşe denenmez. Wildcard fetch, `--all`, `--prune`, refspec, başka remote, global git
flag'leri (`-C`/`-c`/`--no-pager`), env prefix, `cd`, zincir, pipe ve yönlendirme yoktur.
Worker fetch yapmaz ve devralmaz. Claude-only modunda lead fetch denemez; kullanıcı çıktısı
yalnız isteğe bağlı kanıttır.

Başarı: exit 0 ve çıktıda `fatal`/`error`/`rejected` yok. Boş stdout başarısızlık değildir
(güncelleme yoksa `git fetch` sessiz olabilir). Başarılı fetch sonrası aynı kontrolde
`git status --short --branch` ayrı doğrudan çağrıyla alınır.

Kullanıcı isteğe bağlı olarak kendi terminalinde repo kökünde şu çıktıyı ayrı mesajda
paylaşabilir; bu otomatik zorunlu fallback değildir ve başarısız lead çağrısından sonra
otomatik istenmez:

```text
git fetch --verbose origin
git status --short --branch
```

Kullanıcı çıktısı kullanılırsa son tetikleyiciden sonra alınmış, doğru repo/`origin` için başarılı
olmalı ve paylaşılan status dal/upstream/ileri-geri bilgisi lead’in aynı komut çıktısıyla
eşleşmelidir; uyuşmazlıkta yanlış repo/clone veya araya giren değişiklik olasılığıyla dur.
Bu eşleşme tek başına fetch kanıtı değildir. Lead'in kendi workdir çağrısı kanıtında kullanıcı
çıktısı şartı yoktur.

İzin/kimlik/ağ/kota/timeout, sıfır olmayan çıkış veya etkileşimli istem olursa "güncellik
doğrulanmadı" de, "hazırız" deme, iş seçme ve dur. Aynı veya diğer izinli biçimle tekrar,
fallback, worker devri ve izin aşma yok. Kullanıcı sorunu çözdüğünü bildirirse ayrı sonraki
çağrıda tek yeni deneme yapılabilir. Fetch yalnız remote-tracking ref'leri günceller; çalışma
ağacını veya yerel dalı merge etmez. Tek kontrollü başlangıç fetch'i dışında arka plan/sürekli
otomatik fetch yoktur; pull/merge/rebase/reset/stash/dal değiştirme/push otomatik yapılmaz.

Fetch sonrası izinli ayrı doğrudan git çağrılarıyla karşılaştır:
- `git status --porcelain=v2 --branch`: dal, upstream ve ileri/geri bilgisi.
- Upstream varsa `git log --oneline "@{u}..HEAD"` ve `git log --oneline "HEAD..@{u}"`.
- Entegrasyon dalı bu repoda `origin/main`: `git log --oneline origin/main..HEAD` ve
  `git log --oneline HEAD..origin/main`. Feature upstream'i entegrasyon dalı sanma.

Dirty, detached, upstream yok/gone, eksik entegrasyon ref'i veya karşılaştırma hatası
hazır sayılmaz; somut nedeni ve gereken kullanıcı kararını bildir. Upstream'e göre behind
veya diverged ise iş başka yerde ilerlemiştir: commit farklarını ve gerekirse
`git show "@{u}:STATE.md"` ile güncel devam noktasını incele, yerel eski işi başlatma.
Entegrasyon dalındaki ilerleme için ayrıca `git show origin/main:STATE.md` kullan; iki devam noktasını karıştırma.
Upstream'e göre ahead ise pushlanmamış işi açıkla ve varsayılan dur. Feature dalının
`origin/main` üzerinde yalnız ahead olması tek başına hata değildir; izinli dal/kapsamı
belirt. Entegrasyon dalına göre behind/diverged ise uzlaştırma kararı olmadan başlama.

Kullanıcı raporlanan belirli dal/HEAD ve farkı açıkça kabul ederse o kapsamda çalışılabilir;
fetch başarısızlığını veya izin reddini bu kabul ile başarılı sayma. Lead’in kendi commit’leri dışındaki dal/worktree/HEAD değişiminde kabul geçersizleşir. Aynı oturum, aynı worktree/dalda kontrol sonrası yalnız
lead'in kendi commit'leri eklendiyse bu doğal ahead durumu yeni fetch gerektirmez.
Doğrulanan başlangıç HEAD’ini yalnız konuşmada tut; iş başında status ve `git log --oneline <kontrol-HEAD>..HEAD` aralığının yalnız lead’in raporladığı commit’leri içerdiğini doğrula. Aralık/başlangıç HEAD’i açıklanamıyor veya bilinmiyorsa yeni tazelik kontrolüyle dur.
Kalıcı tazelik işareti, kilit/session deposu veya STATE'e tazelik sertifikası yazma.

İş seçimi ancak kontrol sonrası doğru dalın güncel STATE/WORKFLOW/AGENTS içeriğinden
yapılır. Rapor dal/HEAD, uzak karşılaştırma, milestone/alt iş ve sıradaki adımı içerir.
"Oturum doğrulandı" yalnız Git/devam noktası doğrulamasıdır; güncel test/gate, runtime,
Claude giriş/kota veya her makinedeki bağımlılıkların hazır olduğu iddiası değildir.
Yeni komut ve yeni lead izinleri ancak OpenCode yeniden başlatılınca yüklenir; mevcut oturum
reload olmuş sayılmaz.

## Döngü

1. Önce "Oturum başlangıcı ve git tazeliği" kontrolü; geçmezse iş seçmeden dur. STATE ve
   `git status` oku; açıklanmayan değişiklikte dur, başkasının işine dokunma.
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
8. STATE yalnız gerçek devam durumu değişince güncellenir. Review ve kontrollerden geçen iş
   tek otomatik commit olur: görevde gerçekten değişen dosyalar (STATE değiştiyse o da) tek tek
   stage edilir; worker STATE/git yazmaz. Göreve özel açık git yasağına uyulur.
   Komut: `git commit -m <başlık> -m <gövde>`. Başlık `tür(iş-kodu): açıklama`
   (örn. `feat(M6.5a): ...`, `chore(WF-S1): ...`). Gövde kısa olur: milestone/alt iş,
   davranış ve gerekçe; gerçekten yapılan kontrol/sonuç ve atlanan önemli kontrol; review ve
   kalan önemli sınır. Gerekli requested/observed model doğrulama ayrıntısı da burada yazılır,
   STATE'te değil; ham çıktı/log/session kimliği yazılmaz. Sırf kendi hash'ini kaydetmek veya
   biten işi tekrar anlatmak için STATE-only commit yoktur; gerçek karar/engel/review veya gate
   kapanışı için olabilir. Amend/history rewrite yok, eski commit'ler değişmez.
   Push yalnız açık kullanıcı talebiyle.

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
bash scripts/claude-agent.sh implement 'Komut bakımı: continue.md/oturumabasla.md gövdesi...' --maintain-commands --log
```

`--maintain-commands` opt-in komut bakımıdır; yalnız `implement` ile ve `--resume`
olmadan kabul edilir, aksi provider başlamadan CLI hatasıdır (exit 2; doğrudan API
`ValueError`). Kısaltılmış seçenekler (`--maint`, `--mod`) kabul edilmez. Yol listesi veya
wildcard parametresi yoktur; hedef yalnız mevcut `.opencode/commands/continue.md` ve
`.opencode/commands/oturumabasla.md` gövdeleridir. Kullanım koşulları:
- Her görev için açık kullanıcı onayı gerekir; lead kendiliğinden başlatmaz.
- Resume yoktur: düzeltme turu aynı modelle yeni çağrıdır; bakım session'ı başka modda
  resume edilmez. Session deposu yoktur.
- Model seçimi aynıdır (Sonnet varsayılan, `--model opus` isteğe bağlı); izinler family'ye göre
  değişmez. Hata/denial/mismatch başarısızdır, retry/fallback yoktur.
- Prompt yalnız gövde değişikliğini ister; ilk `--- ... ---` frontmatter bayt bayt korunur ve
  rapor "frontmatter unchanged" der. Bu prompt şartıdır, teknik alan-sınırı değildir; lead
  diff'i okur, GPT reviewer bağımsız inceler, gerektiğinde salt-okunur Opus güvenlik review'u
  yapılır.
- Komut gövdesi lead talimatıdır. Bakım diff checklist'i: frontmatter bayt bayt aynı; yeni
  `` !`...` `` shell template veya `@dosya` referansı yok; `$ARGUMENTS` kapsamı korunur;
  izin/ajan/model/araç/commit/push/fetch yetkisi açık görev onayı ve mevcut WORKFLOW sınırları
  dışında genişlemez. Onaylı fetch akışıyla uyum için mevcut metin güncellenebilir. Bu
  prompt + diff review politikasıdır; teknik enforcement yoktur.
- Normal `implement`/`consult`/`review` argv, izin ve prompt'u değişmez. Bakım akışı yalnız
  fake testlerle sınanmıştır; canlı izin davranışı doğrulanmamıştır.

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
Tek komutlu izleme: ikinci terminalde bir kez
`bash scripts/claude-watch.sh --follow-latest` çalıştırılır; lead Claude çağrıları `--log`
ile başlatılmalıdır. Mod tmp/claude altındaki en yeni (dosya adı sıralı) logu baştan
gösterir, her saniye yeni log arar, yeni log çıkınca (eskisi bitmemiş olsa da) başlık
basıp ona geçer ve ekranı temizlemez. Log yoksa dizini oluşturmadan “Yeni görev
bekleniyor” der; final result'ta çıkmaz, bekleme mesajını bir kez basıp yeni görevi
bekler (Ctrl+C ile çıkılır). Böylece OpenCode araç çıktısının gecikmesi yüzünden WATCH
dosya adını beklemek gerekmez. Mod yalnız okur; tekli `claude-watch.sh <log>` kullanımı
ve final result'ta çıkışı aynen sürer; ikisi birlikte veya hiçbiri verilmesi CLI hatasıdır.
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
- OpenCode GPT reviewer için komut sözleşmesi `.opencode/agents/reviewer.md` içindedir; Claude consult/review wrapperına uygulanmaz (o rolde Bash yoktur). Lead her GPT reviewer görev promptuna şu kısa sözleşmeyi ekler: “Git yalnız ayrı doğrudan `git status/diff/show/log` çağrılarıyla, çalışma dizini araç `workdir` parametresiyle; global `--no-pager`/`-C`/`-c`, env prefix, `cd`, zincir, pipe, yönlendirme, `cat`/`head`, `difftool`/`--output` yok; okuma Read/Grep/Glob ile. Ret olursa tekrar deneme veya başka yolla aşma; tam komut + dizin + maskeli ham hatayla dur, genel shell izni yok sonucu çıkarma. Eksik review onay değildir; `review eksik` yaz.”
- Her iki implement modeli aynı normal Edit/Write ve yalnız proje test/lint Bash izinlerini alır:
  backend `.venv/bin/pytest`, `.venv/bin/ruff check`, `.venv/bin/mypy`; frontend
  `npm test`, `npm run lint/typecheck/build/check:contrast`. İlgili backend/frontend
  dizinine ayrı `cd` ile geçebilir; zincir/pipe/yönlendirme yoktur. Git yazma,
  destructive shell, başka CLI/ajan komutu allow listesinde değildir; `dontAsk`
  izin isteyen diğer eylemleri reddeder. STATE ve kalıcı ajan config düzenlemesi yasaktır.
  Normal implement deny listesi `.opencode/**` Edit/Write'ı kapsar.
- Tek istisna `--maintain-commands` bakım setidir; normal rollere eklenmez. tools yalnız
  Read/Grep/Glob/Edit; allow yalnız Read/Grep/Glob ile
  `Edit(./.opencode/commands/continue.md)` ve `Edit(./.opencode/commands/oturumabasla.md)`.
  Genel Edit/Write/Bash ve test izinleri yoktur; Agent/Task/Skill/MCP/Bash/Write disallowed'dır.
  Deny: `.env` okuma kuralları, `STATE.md`, `.claude/**`, `.git/**`, `.opencode/agents/**`.
  Geniş `.opencode/**` deny bu ayrı sette yoktur (iki allow'u öldürürdü); `dontAsk` allowlist
  dışını reddettiği için genel `.opencode` yazma açılmaz. Bu iki gövde, kalıcı ajan config
  yasağının kullanıcı onaylı dar istisnasıdır; ajan tanımları, komut frontmatter'ı ve diğer
  config yasak kalır. Bu izinler de OS sandbox değildir.
- Bakım seçeneğini ekleyen tek seferlik bootstrap tamamlandı; izni tüketildi/KAPALI.
  scripts/test/WORKFLOW için bootstrap izni yeniden kullanılamaz; lead.md içindeki eski
  bu bootstrap ifadesi yeni izin vermez.
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

## Geçici Claude-only modu

OpenAI/GPT kotası bittiğinde, yalnız kullanıcı isterse. Normal akış (OpenCode lead) değişmez;
kota açılınca geri dönülür.

**Geçiş.** Kullanıcı `/claude-only` yazar (`bash scripts/mode.sh claude-only` çalışır). Betik
`.claude/modes/claude-only.settings.json` dosyasını `.claude/settings.json` üzerine kopyalar:
`STATE.md` düzenleme ile `git add/commit` yasakları kalkar, `git push`, `opencode` ve
`commit --amend` yasakları kalır. Mod dosyaları (`.claude/modes/normal.settings.json` ve
`claude-only.settings.json`) kaynaktır; ayarlar settings.json'a yalnız betikle yazılır. Betik
settings.json hiçbir mod dosyasıyla eşleşmiyorsa üzerine yazmaz. İzinler yenilenmezse oturum
yeniden başlatılır (ilk denemede doğrulanacak). `/claude-only` ve `/normal-mod` yalnız kullanıcı
tarafından çağrılır; model bu geçişi kendiliğinden başlatmaz. Ardından `/devam-claude` döngüyü yürütür.

**Roller.** lead: Claude Code ana oturumu. danışman ve implementer: değişmez (adapter, Opus/Sonnet).
reviewer: Sonnet kodunu Opus `scripts/claude-review.sh` ile inceler; **Opus kodu bu modda
review edilmez**, commit gövdesinde "GPT review bekliyor" yazılır ve kullanıcıya bildirilir.
STATE'te iş başına review/hash listesi yok. Luna/checker yoktur; lead hedefli kontrolleri
ve milestone gate'lerini kendisi çalıştırır. Diğer tüm kurallar (Opus görüşü, tek yazıcı, bir review
düzeltme turu, “Uygulayıcı: ...” duyurusu, otomatik commit, push yalnız kullanıcı) aynen geçerlidir.

**STATE işareti.** İlk gerçek işten önce mevcut HEAD'in tam SHA'sı tek başlangıç sınırıdır;
`Şu an` bölümünde ilk gerçek işle aynı commit'te mevcut Mod satırının YERİNE
`Mod: Claude-only; GPT review başlangıcı: <SHA>` yazılır; `<SHA>` yalnız tam 40 hex commit
kimliğidir. Bekleyen review başlangıcı varsa her durumda korunur, yeniden girişte sıfırlanmaz. İş yoksa yalnız işaret
için commit yoktur; commit sonrası kendi hash'i yazılmaz. İşaret mod izni veya tazelik
sertifikası değildir. Yalnız lead yazar.

**Dönüş.** Kullanıcı `/normal-mod` yazar (`bash scripts/mode.sh normal`); STATE yasağı geri gelir.
Sonraki ilk OpenCode `/continue`'da lead başlangıç sınırının mevcut HEAD'in atası olduğunu izinli
git log/show ile doğrular (kısa SHA, dal/etiket adı veya başka seçenek kabul edilmez): salt `git log <sha>..HEAD` başarısı atalık kanıtı değildir; tam SHA
HEAD geçmişinde bulunmalıdır. Aralıkta merge/pull veya açıklanamayan geçmiş varsa dur, işareti koru.
Sınırdan sonraki bütün commit'ler (Sonnet/Opus dahil) bağımsız GPT review'a girer; bulgular normal
sınıflara göre işlenir. Temiz review ve gerekli düzeltmelerin doğrulaması bitmeden işaret kalkmaz;
review eksik veya must-fix varsa başlangıç korunur. İşareti kaldırmak gerçek STATE-only kapanış
commit'i olabilir.

## Gate ve STATE

Küçük işlerde yalnız görevdeki hedefli kontroller. Milestone kapanışında checker:
backend `.venv/bin/ruff check .`, `.venv/bin/mypy app tests`, `.venv/bin/pytest`;
frontend `npm run lint`, `npm run typecheck`, `npm test`, `npm run build`,
`npm run check:contrast`. Her biri kendi backend/frontend çalışma dizininde.
Uygulama gate komutları aynen geçerlidir; yalnız belge işi uygulama gate'i gerektirmez.

STATE kısa, tek kaynaklı aktif durumdur: hedef ~60, kapanış üst sınırı 80 fiziksel satır.
İçerik yalnız: aktif durum/devam noktası, sıradaki bounded iş, gerçekten açık karar/engel,
çözülmemiş bulgu ve hâlâ geçerli kısa risk, en son ilgili gate özeti. Eski sonuçlara ekleme
değil yerine yazılır; gate özeti tarih + makine/sınır taşır ve kaynak değişince bayattır.
İş geçmişi commit gövdesindedir; onaylı kalıcı karar yetkili spec/mimaride, kalıcı iletişim
tercihi AGENTS'te durur; git status, probe listesi, hash eşlemeleri, log/session kimliği ve ham
çıktı STATE'te yoktur. İş kodu görev/session kimliği değildir; STATE aktif iş için kullanabilir.
Lead ve bağımsız reviewer her kapanışta STATE'i Read ile satır sayısı ve güncelliği için
kontrol eder; sığmazsa canlı bulguyu silmek veya dev satıra sıkıştırmak yerine sınıflandırma/karar
için dur. Yeni journal/backlog/history dosyası ve kalıcı sayaç yoktur. Yalnız lead yazar.
