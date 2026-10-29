# Durum

Güncel özet; iş akışı `docs/WORKFLOW.md`. Son güncelleme: 2026-10-29.

## Şu an

- M0–M5 kabul edildi (M5: playlist/album, `d37b78f`); child dosya izni artık `0644`.
- OpenCode: GPT-6.1 Sol lead/reviewer; Sonnet varsayılan, Opus uzman implementer; Luna mekanik/checker.
- Danışman/review Opus kalır. Lead uygulayıcıyı ve gerekçesini iş başında açıklar;
  model değişimi sessiz yapılmaz, implement resume açık --model ister.
- İnce CLI adaptörü kuruldu: özel görev deposu, kilit ve test yürütücüsü yok; isteğe bağlı canlı log var.
- Otomatik izleme: `bash scripts/claude-watch.sh --follow-latest`; en yeni logu gösterir, yeni göreve
  geçer, result sonrası bekler. Claude çağrıları `--log` kullanmalı; Ctrl+C yalnız izleyiciyi kapatır.
- Gerçek salt-okunur Opus review ve aynı oturuma dönüş PASS: `claude-opus-5-5`, izin reddi yok.
- Gerçek Sonnet uygulama/test döngüsü PASS: `claude-sonnet-5-5`, yalnız izinli test dosyası değişti;
  pytest/ruff/mypy komutlarını kendisi çalıştırdı, açık aynı-model resume takip çağrısı PASS, izin reddi yok.
- Otomatik izleyici Sonnet ile geliştirildi; bağımsız GPT review: must-fix yok;
  lead hedefli kontrolleri tekrar doğruladı. Uygulamanın indirme davranışı değişmedi.
- M6 başlangıç Opus görüşü alındı; requested opus / observed claude-opus-5-5 eşleşti,
  çağrı başarılı ve izin reddi yok. Uygulama başlamadı; ürün tercihleri bekleniyor.

## Sıradaki iş

1. Kullanıcı M6 ürün tercihlerini yanıtlasın; ardından tek bounded uygulama işi seçilsin.
2. Opus planı: kaynak metadata → merkezi güvenli yollar → playlist video adları →
   staging'de etiket/remux → müzik yolları/katalog → kapak → metadata düzenleme → kapanış gate.
3. Lead: mevcut staging/yayınlama sınırlarını koru; kaynak alanlarını tahmin etmeden taşı.
   Playlist adı otomatik albüm değildir; albüm alanı tek başına küçük resmi güvenilir yapmaz.
   Kayıpsız remux kaliteyi korur; yeniden kodlama ve başlık temizliği kayıp riski taşır.
   Sonraki aday: kaynak metadata modeli/normalizasyonu; API sözleşmesi değişirse Opus.
Kural: review ve kontroller sonrası görev başına otomatik commit; push yalnız açık taleple.

## Açık kararlar

- M6 (henüz onaylanmadı): doğal ses kabı mı, kayıpsız WebM/Opus → .opus remux mı?
- Eksik alanlarda öneri: metadata uydurma; sanatçı/albüm yoksa düz yerleşim, parça adı için kaynak başlığı.
- Numara önerisi: koleksiyonda özgün kaynak sırası; başlıkta otomatik ek temizliği yapma.
- Kapak önerisi: harici arama yok; belirsiz kaynak küçük resmi yalnız açık kullanıcı seçimiyle.
- Etiket/kapak/remux hatası önerisi: güvenli orijinali koru, uyarıyla tamamla; iptal, güvenlik,
  depolama veya katalog hatalarını bu fallback kapsamında yutma. Gömme biçimleri uygulamada netleşecek.
- M9: video kalite basamaklarını sadeleştirme ve boyut gösterimi.
Kaynaklar: PRODUCT_SPEC §13–16, ARCHITECTURE §30–34 ve §59 M6.

## Açık bulgular

- should-fix: boş kuyrukta container SIGTERM sonrası 10 sn içinde kapanmadı; Podman SIGKILL kullandı.
- should-fix: Fedora Python 3.14'te mevcut 3 backend test hatası (aşağıda).
- Uygulayıcı dosya kapsamı prompt kuralıdır. Sonnet uygulama/test ve aynı-rol oturum dönüşü görüldü;
  Opus uygulama/test ve farklı-rol resume izolasyonu denenmedi. Native bg/attach kullanılmıyor.
- should-fix: WORKFLOW kullanımına, modelsiz implement resume'ın CLI hatası verdiği açıkça eklenebilir;
  aynı modelin açık yeniden seçimi zaten kuraldır. Gözlenen model yalnız init'ten, lead de doğrular.
- should-fix: izleyicinin partial-line testinde buffer adımları daha deterministik doğrulanabilir;
  reviewer uygulamada ilgili bir hata görmedi, otomatik takip için bloklamıyor.

## Son gate

- 2026-10-29 Fedora, M6 başlangıç planlaması: yalnız salt-okunur inceleme ve Opus danışma;
  uygulama/test/config değişmedi, hedefli test veya full gate çalıştırılmadı.
- 2026-10-29 Fedora, otomatik izleme: worker ve lead pytest `test_claude_bridge.py` 43 PASS;
  test+izleyici ruff ve hedefli mypy PASS. Önceki model seçimi işinde adaptör ruff PASS.
  Bu çağrıda full gate çalıştırılmadı; test değişikliği bağımsız GPT review'dan geçti.
- Önceki full gate: macOS backend ruff/mypy PASS, pytest 1466 PASS; frontend 147 test ve tüm gate'ler PASS.
- Önceki Fedora Python 3.14 full gate: ruff/mypy PASS, pytest 1462 PASS/1 skip/3 FAIL;
  `test_rejected_output_closes_all_walk_descriptors` os.close monkeypatch/pidfd çakışması.
  Frontend tümü PASS. Bu eski sonuçlar yeni test/araç ağacı için güncel full gate değildir.

## Bilinen riskler

- Claude tool izinleri OS sandbox değil; testler kod çalıştırır. Built-in plugin'ler CLI'da kalıyor;
  canlı review araçları yalnız Read/Grep/Glob, MCP/skills boş. Kota hatasında sessiz fallback yok.
- Canlı log kaynak metni içerebilir; ignored tmp altında. İzleyiciden çıkış işi durdurmaz;
  asıl lead çağrısının açık kalması gerekir, timeout/interrupt süreç davranışı tam doğrulanmadı.
- Container root; yt-dlp sabit/görünür değil; hedef filesystem hard link desteği istiyor.
- Accepted: Delete File stat/unlink mikro-penceresi; bazı terminal parent error.retryable alanları;
  ertelenmiş cancel 404 ve cancel event temizliği. Elisa M4A açma sorunu oynatıcıya özgü.

## Sonraki fazlar

- Teknik: kapanış süresi, container user, yt-dlp sabitleme/görünürlük, Python 3.12 CI.
- Gerçek smoke: binary/ses/merge-video/playlist PASS; iptal ve yeniden başlatma henüz denenmedi.
- ARCHITECTURE bölme → M6 → M7 Plex → M8 cihaza indirme → M9 cilalama → v0.1 denetim.
