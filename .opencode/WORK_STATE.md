# Active Work State

Bu dosya Lead için kalıcı (durable) çalışma state'idir ve yetkilidir (authoritative).

Session inference, eski checkpoint özetleri veya hafızadaki state bu dosyayla
çeliştiğinde BU DOSYA geçerlidir. Lead her bounded task tamamlandığında bu
dosyayı güncellemekle yükümlüdür.

Son güncelleme: 2026-10-02
Güncelleme nedeni: WF-SIMPLIFY-1 tamamlandı ve accepted: AGENTS.md, 7 agent promptu ve 4 command wrapper'ında küçük/kontrollü natural-language-first sadeleştirme (12 dosyada 330 insertions, 656 deletions, net ~%31 azalma). Roller, model routing, mode/steps ve permission frontmatter değişmedi; production source değişmedi. Independent reviewer ACCEPTANCE PASS; Critical/Important yok. M2 product gates FRESH kalıyor. Remaining Critical/Important boş; commit-ready EVET.

## Active Milestone

Milestone 2 — First Download (ARCHITECTURE.md §59)

M2 kapatıldı (2026-09-29) — tüm Critical/Important bulgular kapalı, milestone review PASS, full quality gates FRESH, commit-ready.

## Completed Bounded Tasks / Findings

- C1 + I3 — atomik no-overwrite finalization ve async cross-filesystem copy
- C2 + I1 — koşullu state transition, claim/cancel yarışları ve subprocess registration gate
- I2 — FFmpeg cancellation cleanup, terminate→grace→kill→wait
- I4 — job creation tek transaction: job kaydı + initial Queued activity tek commit (regresyon testi: test_activity_failure_rolls_back_job_row)
- I5 — [GEÇMİŞ UYGULAMA — R3 ile kabul edildi] remove_staging containment doğrulaması: ensure_within_root ile staging dışına silme engellendi (regresyon testleri: TestRemoveStaging).
- I6 — [GEÇMİŞ UYGULAMA — R1 ve R2 ile kabul edildi] SSE backpressure (düzeltilmiş): transient event'lerde drop-oldest; persisted activity lane dolunca subscriber disconnect + reconnect'te Last-Event-ID DB replay.
- R1 — I6 replay high-water mark + pagination: bağlantı başında tek seferlik HWM (MAX(activity_events.id)); sayfalı replay (limit=200) SQL seviyesinde `last_event_id < id <= HWM` ile sınırlı (list_events_after max_id parametresi); sayfa yine de over-bound satır içerirse skip-and-continue (id <= HWM satırlar asla düşmez, sonsuz döngü koruması var); id <= HWM canlı event'ler dedupe edilir, id > HWM yalnız live buffered path'ten tam bir kez gelir; transient (id: None) asla atlanmaz; endpoint-level 250-backlog, replay-sırasında-canlı-publish ve HWM-üstü-hariç-tutma regresyon testleri eklendi
- R2 — initial Queued activity live publish: JobService'e EventBus enjekte edildi (main.py wiring); create_job commit BAŞARILI olduktan sonra activity gerçek DB id ile activity.created olarak publish ediliyor (job_runner ile birebir şema); rollback yolunda hiç publish yok (yapısal garanti: publish session bloğu dışında, commit sonrası); sıra: commit → publish → notify; expire_on_commit=False ile id erişimi güvenli; testler: açık subscriber'a tam-bir-kez teslim + gerçek DB id eşleşmesi, activity insert fail → subscriber boş
- R3 — I5 staging-root equality / invalid job_id guard: ensure_within_root artık root'a eşitliği de reddediyor (strict, symlink-to-root dahil); job_staging_dir ve remove_staging containment'ten ÖNCE _validated_job_id guard'ı çağırıyor (boş, ., .., /, \, çok segmentli, absolute reddedilir; mkdir/rmtree hiç çalışmadan ValueError); final_target davranışı korundu; yeni testler: root-equality red, symlink-to-root red, geçersiz job_id'lerde staging root'a dokunulmaması (marker doğrulamalı), uuid-hex happy path, final_target strictness
- R4 — ölü persistence testi: _collect_sse_replay'in return'ünden sonra gömülü olan test_activity_events_are_persisted gövdesi aynen korunarak TestSse'ye gerçek test metodu olarak taşındı; pytest artık collect ediyor ve tek başına PASS (tam dosya 49 → 50); production code değişmedi
- R5 (=I7) — first-connect + M2 basic Activity UI: owner-scoped id-ASC Activity REST history; first-connect SSE no-replay/live-only; subscriber response-start öncesi hazır; EventSource open sonrası REST snapshot + live id-dedupe ile gap-free handoff; collapsed structured Activity panel; retryable load/error state; job-switch reset + stale-response guard; SSE error REST refresh; REST/replay/live UTC-aware timestamps; queued cancel commit→activity.created→job.cancelled sırası. Targeted: backend pytest tests/test_jobs.py 57 PASS + ruff/mypy PASS; frontend lint/typecheck + 28 tests + build PASS. Independent reviewer: ACCEPTANCE PASS — Critical/Important finding yok.
- I8 — filename uzantı kaybı / multibyte NAME_MAX fix: yeni sanitize_media_filename (backend/app/utils/filenames.py) son uzantıyı daima koruyor, gövde+uzantıyı ayrı sanitize ediyor, toplam adı 150 UTF-8 baytta karakter sınırında kesiyor (çok baytlı karakter bölünmez); final_target (storage_service.py) bu builder'ı kullanıyor; mevcut sanitize_filename davranışı korundu; testler: uzun ASCII'de uzantı korunur, CJK/emoji bayt bütçesine oturur ve geçerli UTF-8 kalır, çok noktalı adda son sonek uzantı, bütçe tükenirse fallback, uçtan uca job finalization (200 karakter başlık)
- I9 — uzun son uzantı korunmuyor fix: filenames.py'da _MAX_EXTENSION_LENGTH kaldırıldı; son noktadan sonraki whitespace/forbidden/control içermeyen her temiz sonek, uzunluğundan bağımsız uzantı sayılıyor; dejenere uzantıda (bütçeyi aşarsa) nokta + en az 1 gövde baytı garantisiyle karakter sınırında kesim; I8 bayt bütçesi/karakter-sınırı/fallback invariants korundu; test_unreasonable_suffix_is_treated_as_body yerine uzun gövde+uzun uzantı unit/finalization testleri (93 passed)
- I10 — stale-snapshot cancel full-suite flaky fix: kök neden ürün bug'ı DEĞİL — request_cancel subprocess kapanışını beklerken runner cancellation penceresi içinde statüyü cancelled commit edebiliyor; cancel endpoint'inin son authoritative DB okuması meşru olarak downloading VEYA cancelled dönebiliyor; stale queued snapshot'ın aktif işi ezmesi koşullu güncelleme nedeniyle zaten imkânsız. Teşhis kanıtı: fix öncesi solo 5 koşuda 1 fail (aynı assertion). Fix (test-only): response assertion `in {"downloading", "cancelled"}` (asla queued olamaz); final cancelled/error code/staging assertions korundu. Kanıt: fix sonrası solo 3/3 PASS + tam backend suite 192 passed + ruff/mypy PASS
- WF-EVAL-1 — [workflow maddesi — production milestone finding değildir] generic `.opencode/evals` JSONL+Markdown telemetry schema v1.2, salt-okunur evaluator ve `/evaluate`, her terminal bounded-task checkpoint capture, append-only correction/supersession effective dataset, M2 retrospective baseline. Production/backend/frontend değişmedi. Independent workflow reviewer final ACCEPTANCE PASS; Critical/Important yok. Statik/git doğrulamaları PASS; runtime OpenCode/JSON parser mevcut izinlerde çalıştırılamadı (limitation).
- WF-EVAL-APPEND-1 — CLOSED (2026-10-02) — safe deterministic JSONL append helper runtime acceptance tamamlandı: `--self-test` PASS (strict v1.2 valid+invalid fixtures, prefix korunumu, tek-fd append, duplicate/newline/symlink/invalid-UTF-8 reddi, mevcut lock'ta fail-closed), `--verify` PASS (16 kayıt), WF-EVAL-1 retrospektif task_run kaydı `wf-eval-1` safe append ile task-runs.jsonl'a eklendi (17 kayıt), append sonrası `--verify` PASS (17 kayıt, unique id). Statik reviewer ACCEPTANCE PASS (2026-09-30) ile birlikte task tamamlandı; production source değişmedi. Önceki permission-denied durumu `wf-eval-append-1-runtime-acceptance-2026-10-01-blocked` telemetry kaydında duruyor; kök neden checker rolünde Node izni olmamasıydı (hot-reload değil); runtime acceptance fast-worker'ın exact Node komut izinleriyle tamamlandı.
- WF-SIMPLIFY-1 — [workflow maddesi — production milestone finding değildir] AGENTS.md, 7 agent promptu ve 4 command wrapper'ında küçük/kontrollü natural-language-first sadeleştirme. Roller, model routing, mode/steps ve permission frontmatter değişmedi. Product/backend/frontend, opencode.json, telemetry schema/helper/task-runs/evaluator semantiği değişmedi. Per-task telemetry, durable state sırası, provider/permission/orchestration vs implementation ayrımı, independent reviewer/final checker, destructive Git ve Definition of Done sınırları korundu. Command dosyaları ince wrapper oldu; ilgili authoritative kurallar agent promptlarında tek yerde kaldı. Independent reviewer ACCEPTANCE PASS; Critical/Important yok. Kontroller: git diff --check PASS; changed-scope doğrulaması yalnız beklenen 12 prompt/config dosyası PASS; frontmatter model/mode/steps/permission/agent/subtask diff yok PASS; dört command dosyasında $ARGUMENTS PASS; telemetry helper --verify PASS (20 valid unique records). Product full suite çalıştırılmadı (production değişikliği yok). Diff: 12 dosyada 330 insertions, 656 deletions, net ~%31 satır azalması.

## Remaining Bounded Tasks / Findings

Boş — M2 ve workflow task'ları (WF-EVAL-1, WF-EVAL-APPEND-1, WF-SIMPLIFY-1) için açık Critical/Important finding veya bekleyen bounded task yok.

M2 için kaydedilmiş Critical/Important finding kalmadı (I10 kapatıldı); WF-EVAL-1, WF-EVAL-APPEND-1 ve WF-SIMPLIFY-1 workflow task'ları için de açık Critical/Important finding yok. Residual low-risk gözlemler (blocker değil, backlog adayı): job_service cancel 404 code 'analysis_not_found' ileride 'job_not_found' olabilir; queue_manager _cancel_events uzun ömürlü kullanımda temizlenmeli; çok-kullanıcılı gelecekte SSE replay owner-scoped olmalı; checker docker compose logs izni şimdi mevcut (checker.md güncellendi), sonraki closure koşusunda migration 0003 log düzeyinde doğrulanabilir.

## Last Verified Quality Gates (2026-09-29)

Aşağıdaki sonuçlar geçmiş doğrulama kaydıdır (historical record). Bu sonuçlar
açık Critical/Important finding'leri resolved YAPMAZ.

**FRESH (2026-09-29, Milestone 2 closure checker):** Current working tree üzerinde tüm kapılar PASS — Backend: `ruff check .` PASS, `mypy app tests` PASS (56 dosya), `pytest` **192 passed** (I10 fix sonrası ilk formal full koşu); Frontend: lint PASS, typecheck PASS, tests **28 passed**, build PASS; Docker: build PASS, container Up **healthy**, `/api/v1/health` PASS (`database: ok`), SPA PASS, migration log doğrulaması PASS (Alembic çalıştı, hata yok; 'Running upgrade' satırı görünmedi — kalıcı volume DB'si zaten güncel revision'da, limitation kayıtlı), `docker compose down` temiz kapanış. Kontroller sırasında kaynak dosyalar değişmedi (git status öncesi/sonrası birebir aynı). Engelleyici olmayan 2 deprecation uyarısı (Starlette TestClient httpx, sqlite datetime adapter) follow-up adayı.

Not (2026-09-29, WF-EVAL-1 workflow task): Production source değişmediği için yukarıdaki M2 closure checker kaydı STALE değildir; FRESH geçerliliğini korur. Bu workflow görevinde full backend/frontend suite yeniden koşulmadı; production değişikliği olmadığından gerekmedi.

Not (2026-09-30, WF-EVAL-APPEND-1 workflow task): Production source değişmedi; M2 product gates FRESH geçerliliğini korur.

Not (2026-10-02, WF-EVAL-APPEND-1 runtime acceptance): Production source değişmedi; M2 product gates FRESH geçerliliğini korur. Runtime telemetry helper doğrulamaları: `--self-test` PASS, `--verify` PASS (append öncesi 16, sonrası 17 kayıt).

Not (2026-10-02, WF-SIMPLIFY-1): Production source değişmediği için yukarıdaki M2 closure full gates FRESH geçerliliğini korur; product full suite çalıştırılmadı (gerekmedi). Yalnız statik prompt kontrolleri koştu: git diff --check PASS; changed-scope doğrulaması (yalnız beklenen 12 prompt/config dosyası) PASS; frontmatter model/mode/steps/permission/agent/subtask diff yok PASS; dört command dosyasında $ARGUMENTS PASS; telemetry helper --verify PASS (20 valid unique records).

Önceki kayıt (2026-09-28, historical — M2 geliştirme öncesi tam koşu):

- Backend: `ruff check .` PASS, `mypy app tests` PASS, `pytest` 147 PASS
- Frontend: `npm run lint` PASS, `npm run typecheck` PASS, `npm test -- --run` 14 PASS, `npm run build` PASS
- Docker: `docker compose build` PASS, container `(healthy)`, `/api/v1/health` PASS, SPA PASS, `docker compose down` ile temiz kapanış; migration 0003 container başlangıcında hatasız uygulandı
- Kontroller sırasında kaynak dosyalar değişmedi (git status öncesi/sonrası birebir aynı)

Gate geçerlilik kuralları:

- Bu kayıt yalnızca kaydedildiği working tree için geçerlidir.
- Herhangi bir production source değişikliği yapıldığında önceki full milestone
  gate sonuçları CURRENT working tree için STALE sayılır.
- Bounded worker her task'ta yalnız task'ına ait targeted testleri çalıştırır;
  her task sonrası full suite çalıştırılmaz.
- Remaining Critical/Important listesi boşaldığında, milestone commit-ready
  olmadan önce güncel working tree üzerinde reviewer/checker akışı ve fresh
  full quality gates tekrar çalıştırılmalıdır.

## Next Bounded Task

Yok. M2 kapalı; WF-EVAL-1, WF-EVAL-APPEND-1 ve WF-SIMPLIFY-1 workflow task'ları tamamlandı. M3 yalnızca kullanıcının açık yeni talebiyle planning/implementation olarak başlayabilir. Telemetri altyapısında somut bir append hatası görülmedikçe yeni hardening/refactor/özellik yapılmayacak. Backlog adayı residual low-risk gözlemler Remaining bölümündeki notlarda duruyor.

## Commit-Ready Durumu

Commit-ready: EVET (2026-10-02) — WF-SIMPLIFY-1 tamamlandı ve accepted.

1. Remaining Critical/Important findings: BOŞ ✓ — M2 findings boş; WF-EVAL-1, WF-EVAL-APPEND-1 ve WF-SIMPLIFY-1 workflow task'ları için açık Critical/Important yok
2. Review: KAPALI ✓ — M2 milestone review/checker kapalı (retry 2, I9 doğrulandı; I10 checker flake'i resolver ile kapatıldı); WF-EVAL-1 bağımsız reviewer ACCEPTANCE PASS; WF-EVAL-APPEND-1 independent static reviewer ACCEPTANCE PASS (2026-09-30) + runtime acceptance PASS (2026-10-02); WF-SIMPLIFY-1 independent reviewer ACCEPTANCE PASS (2026-10-02, Critical/Important yok)
3. Current working tree production gates: FRESH ✓ — production source değişmedi (WF-SIMPLIFY-1 yalnız 12 prompt/config dosyasını değiştirdi); M2 closure full quality gates (2026-09-29) geçerliliğini korur

Not (2026-10-02, Git closure attempt): Kullanıcı commit talep etti; working tree incelendi (yalnız workflow/telemetry değişiklikleri: .gitignore, .opencode/WORK_STATE.md, .opencode/agents/fast-worker.md, .opencode/agents/lead.md, .opencode/agents/evaluator.md, .opencode/commands/continue.md, .opencode/commands/evaluate.md, .opencode/evals/SCHEMA.md, .opencode/evals/append-task-run.mjs, .opencode/evals/reports/M2.md, .opencode/evals/task-runs.jsonl; production/backend/frontend değişikliği yok), `git diff --check` temiz, hedefli stage listesi belirlendi. Ancak Lead session bash izinleri salt-okunur git komutlarıyla sınırlı ve tüm worker agent'ları (fast-worker, implementer) izin ve prompt kuralları gereği stage/commit'ten men; stage+commit kullanıcı tarafından çalıştırılmalı veya izin config güncellemesi sonrası tekrar denenmeli. Önerilen commit mesajı: 'chore: add agent workflow performance evaluation'.

Not (2026-10-02, Git permission update): Lead config güncellendi — `git add` ve `git commit` allow, `git commit --amend` deny eklendi; push/reset/clean/checkout/restore/rebase implicit deny ile engelli kalmaya devam ediyor; worker rollerine Git sorumluluğu eklenmedi. Config değişikliği mevcut oturumda etkili değildir; opencode restart sonrası Lead normal Git closure'u kendisi tamamlayabilir. Bekleyen hedefli stage listesi ve önerilen commit mesajı yukarıdaki Git closure attempt notunda durur.

Not: Commit/push yalnız kullanıcının açık talebi üzerine yapılır; Lead ve worker'lar kendi başına commit atmaz.
