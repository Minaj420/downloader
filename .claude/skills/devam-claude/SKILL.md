---
name: devam-claude
description: GPT kotası bittiğinde Claude Code'u geçici lead yapar (Claude-only modu); yalnız kullanıcı /devam-claude yazarsa
disable-model-invocation: true
---

# /devam-claude — geçici Claude-only lead

Türkçe cevap ver. Bu, OpenCode `lead` döngüsünün (`.opencode/agents/lead.md`,
`.opencode/commands/continue.md`) kota geçici olarak biterken Claude Code'a taşınmış halidir.
Kurallar `docs/WORKFLOW.md`'dedir; "Geçici Claude-only modu" bölümü bu skill'in kaynağıdır.

## Başlamadan

1. `bash scripts/mode.sh status` çalıştır. `claude-only` değilse dur: kullanıcıya `/claude-only`
   yazmasını söyle. İzinleri kendin değiştirme.
2. `STATE.md`, `docs/WORKFLOW.md`, `git status` oku. Açıklanmayan değişiklikte dur.
3. İlk gerçek işin commit'i için STATE'teki "Şu an" bölümünde
   `Mod: Claude-only; GPT review başlangıcı: <ilk işten önceki HEAD'in tam SHA'sı>`
   kaydını hazırla. Bekleyen başlangıç zaten varsa koru; yeniden girişte sıfırlama.
   Bu kayıt fetch/mod izni kanıtı değildir. İş yoksa yalnız işaret için commit oluşturma.

## Döngü (tek iş; kullanıcı sayı verdiyse en fazla o kadar, üst sınır 5)

- Milestone öncesi `bash scripts/claude-agent.sh consult '<odak>' --log` ile Opus görüşü zorunlu;
  kullanıcıya "Opus önerisi / gerekçe / alternatif-risk + lead kararı" özetini ver.
- Her iş başında “Uygulayıcı: Sonnet/Opus; gerekçe...” de. WORKFLOW'daki seçim kuralları aynen geçerli.
- Sen production kodu, test veya config yazmazsın; işi `bash scripts/claude-agent.sh implement '<görev>' --log`
  ile Sonnet/Opus'a ver. Doğrudan yazabildiğin tek dosya `STATE.md`'dir. Promptu tek argüman olarak
  POSIX shell-quote et; eval, pipe veya yönlendirme kullanma.
- Hedefli kontrolleri worker çalıştırır; sen sonucu kendin tekrar çalıştırarak doğrularsın.
  Luna/checker yoktur: milestone gate'lerini de sen çalıştırırsın.
- Review (GPT reviewer yoktur):
  - Sonnet kodu: `scripts/claude-review.sh '<odak>' --log` ile salt-okunur Opus review.
  - Opus kodu: bu modda review yapılmaz; commit gövdesinde "GPT review bekliyor"
    belirt ve kullanıcıya bildir. STATE'te iş başına ayrı review satırı oluşturma.
  - En fazla bir düzeltme turu; aynı modelle `--resume`, `--model` açıkça yeniden iletilir.
  - Normale dönüşte başlangıç sınırından sonraki bütün commit'ler GPT review kapsamındadır.
- WORKFLOW'un commit ve STATE kurallarını uygula: STATE'i yalnız gerçek durum değiştiyse
  güncelle; ilk review başlangıcını ilk işle aynı commit'te kaydet. Yalnız değişen görev
  dosyalarını tek tek `git add -- <dosya>` ile stage et; `tür(iş-kodu): açıklama`
  başlığı ve kısa iş/kontrol/review gövdesiyle `git commit -m <başlık> -m <gövde>` kullan.
  Commit sonrasında kendi hash'ini STATE'e ekleme; commit listesi tutma.
  Push, amend, geçmiş değiştirme yok; push yalnız kullanıcı isterse ve kullanıcı yapar.
- Kota/kimlik/izin hatasında tekrar veya sessiz model fallback yok.
  Devamı engelliyorsa STATE'teki engel özetini güncelle; çağrı tarihçesi ekleme ve dur.
- Bitince kısa Türkçe özet ver (uygulamanın davranışı üzerinden, kod değil) ve dur.
- OpenCode/başka ajan çağırma, `.claude/settings.json` ve mod dosyalarını düzenleme/commit etme.
