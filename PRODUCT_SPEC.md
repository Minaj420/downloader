# Downloader — Product Specification

**Version:** v0.1  
**Status:** Approved for implementation  
**Product type:** Self-hosted media downloader  
**Primary deployment:** Docker on a private homelab, accessed over Tailscale

---

# 1. Product Goal

Downloader is a self-hosted web application that provides a clean graphical interface around `yt-dlp` and `FFmpeg`.

The application allows a user to:

1. Paste a supported media URL.
2. Analyze the media before downloading.
3. Choose between audio and video when applicable.
4. Select only formats and quality options that make sense for the actual source.
5. Download single items, playlists, or albums.
6. Save files to:
   - the application's own library,
   - Plex Music,
   - Plex Videos,
   - or the device currently using the web application.
7. Track active and queued jobs.
8. Inspect completed downloads in a structured media library.
9. Observe background activity through an optional terminal-like Activity panel.

The application must remain useful even without Plex. Files produced by Downloader should be organized, human-readable, and portable.

---

# 2. Core Product Principles

## 2.1 Source quality first

Downloader must not pretend to improve media quality.

The application should inspect available source formats and derive the available choices from them.

Examples:

- A source with a maximum video resolution of 1080p must not offer 1440p or 2160p as if they were genuine source qualities.
- Audio conversion options must not be presented as higher-fidelity versions of the source.
- `Original / Best Available` should normally be the recommended option.

Transcoding may be offered for compatibility, but the UI must clearly treat transcoding as conversion rather than quality enhancement.

No artificial upscaling is required in v0.1.

---

## 2.2 Plex-compatible, not Plex-dependent

Media organization must remain meaningful outside Plex.

A file copied manually to another computer, phone, NAS, Jellyfin server, USB drive, or another media player should still have a useful filename and metadata.

Plex is a destination, not the application's fundamental storage model.

---

## 2.3 Simple by default, advanced when needed

The normal workflow should not expose unnecessary `yt-dlp` or `FFmpeg` complexity.

Technical information should remain accessible through the Activity panel and future advanced features.

---

## 2.4 Private-first deployment

v0.1 is intended to run on a private homelab and be accessed through Tailscale.

The application does not require its own authentication system in v0.1.

However, the architecture must not make future user authentication unnecessarily difficult to introduce.

---

# 3. Supported Sources

Downloader should function as a generic frontend for `yt-dlp`, rather than being designed exclusively around YouTube.

Examples of relevant platforms include:

- YouTube
- YouTube Music
- SoundCloud
- Bandcamp
- Vimeo
- Twitch
- TikTok
- Instagram
- X / Twitter
- Reddit
- Other URLs supported through `yt-dlp`

Support is dependent on the installed `yt-dlp` version and the current state of individual extractors.

The application must not implement DRM circumvention.

Users are responsible for downloading media they are permitted to access and save.

---

# 4. Platform Selection

The New Download screen must include a platform dropdown.

Default:

`Auto Select`

Example:

```text
Platform
[ Auto Select ▼ ]

URL
[ https://... ]

[ Analyze ]
```

Possible platform choices:

```text
Auto Select
────────────
YouTube
YouTube Music
SoundCloud
Bandcamp
Vimeo
Twitch
TikTok
Instagram
X / Twitter
Reddit
Other
```

## Auto Select

This is the default and expected normal workflow.

The backend should let `yt-dlp` determine the appropriate extractor.

## Manual selection

The user may explicitly select a platform.

Manual selection acts as an override/hint rather than creating an entirely separate downloader implementation.

If the selected platform conflicts with the detected source, the application should provide a useful correction rather than failing unnecessarily.

Example:

```text
Selected platform: YouTube
Detected platform: SoundCloud

[ Use SoundCloud ]
```

**Auto Select display and platform icons (approved; planned, not implemented).** After analysis, the dropdown shows the detected platform while the mode stays Auto Select; displaying the backend's detected hint must never silently turn into a manual override (the platform hint sent with a later analysis stays absent unless the user explicitly picks a platform). Platform icons are approved for v0.1 and come from an openly licensed icon set; the actual set and license are **still open** (no candidate chosen; selection/research awaits approval, no dependency is added now). The preview platform badge keeps brand colour/logo with the existing contrast check, and an icon is never the only semantic carrier of the platform (the text label stays).

---

# 5. Extractor Failure Handling

There will be:

- no background extractor health polling,
- no periodic health checks,
- no "last successful use" dashboard,
- no extractor uptime history in v0.1.

Extractor validity is determined during real user actions.

When an extractor fails, the user must receive a human-readable application message.

Example:

```text
Could not analyze this URL

The selected extractor could not retrieve media information.

Possible causes:
• The site may have changed
• yt-dlp may need an update
• The content may require authentication
• The URL may not currently be supported

[ Retry ] [ View Activity ]
```

More specific errors should be shown when they can be determined reliably.

Examples:

- Authentication required
- Unsupported URL
- Geo restriction
- Media unavailable
- Extractor error
- Network error

Raw Python exceptions should not be used as the primary user-facing error message.

Technical details may appear in the Activity panel.

The `[ View Activity ]` action above stays a requirement. Removing it from the analysis-failure example is only a **PROPOSED** spec change from the user-transferred spec audit (ARCHITECTURE §59 M6-Q); nothing is removed until the user approves it. Masked extractor error detail is finding 19 in the approved order.

---

# 6. New Download Flow

The main workflow is:

```text
Select platform
        ↓
Paste URL
        ↓
Analyze
        ↓
Detect source type
        ↓
Single item OR playlist/album
        ↓
Choose Audio / Video
        ↓
Choose quality / format
        ↓
Choose destination
        ↓
Add to Queue
        ↓
Download
        ↓
Post-process
        ↓
Save
        ↓
Library / Device
```

**Single-item Audio / Video choice (approved; planned, not implemented).** For a single item, the top Audio / Video choice works like the collection one: only the source and output options of the selected type are shown.

**New Download state across navigation (observed; draft restore approved; planned, not implemented).** The analysis, selection and job card are lost on tab navigation or F5. The Queue page (§17) restores access to jobs from the server but does not by itself solve this. Approved (user decision): a `sessionStorage`-based draft restore for New Download. It is not blocking: it is scheduled in Queue Q2 where it fits, or after v0.1 if needed. The technical sketch (only `analysis_id` plus minimal identifiers, safe clear on 410/expired) in ARCHITECTURE §59 (Q2) stays a proposal, not an approved design. Approved condition: after a new analysis, the old job card may be cleared only once Queue actually delivers access to cancelled single-job Retry (§17; the history lives in Queue).

**Remember last selection (approved for v0.1; planned, not implemented).** New Download remembers the user's last choices in the browser (`localStorage`); there is no server-side preference storage and Settings defaults stay fixed in v0.1. Only choice identifiers are kept, never raw URLs or full metadata. A remembered choice is applied only when it is still available for the current source; otherwise the default is used. This is separate from the draft restore above, and its v0.1 status does not depend on whether draft restore slips. Technical details in ARCHITECTURE §47.

---

# 7. URL Analysis

No download should begin immediately after pasting a URL.

The application must first analyze it.

The analysis should attempt to retrieve:

- detected platform,
- extractor,
- media title,
- thumbnail where available,
- uploader / artist,
- duration,
- available audio formats,
- available video formats,
- available resolutions,
- codecs where useful,
- estimated file size where available,
- playlist/album information,
- number of playlist items,
- track metadata where available.

The UI should then be generated from actual source information.

---

# 8. Audio Downloads

Audio mode should support source-aware choices.

The preferred default should be:

`Original / Best Available`

Source selection and output choice are separate. The source selection picks which available source stream is downloaded; the output choice picks the final audio format. Required first audio output set for new downloads (approved; planned, not implemented):

- `Original / Best Available` (default)
- MP3
- WAV
- M4A
- Opus
- FLAC

Each format uses one fixed quality profile; v0.1 has no bitrate slider. Exact codec, bitrate and quality parameters of those profiles are a later technical design item and are not decided here.

Conversion is offered for compatibility, not quality. WAV and FLAC do not improve a lossy source, and a lossless target does not restore quality already lost in the source. By default `Original / Best Available` avoids unnecessary re-encoding; other output choices are not a quality increase. Copy/remux and lossless conversion can also preserve quality, while lossy re-encoding can add further loss, and no conversion restores quality already lost in the source. A different output choice does not by itself imply forced re-encoding or loss; the UI must still describe it as a conversion rather than an improvement.

For a playlist/album job, Audio / Video mode and the output format/profile are chosen once at collection level, defaulting to `Original / Best Available`. The backend resolves that preference against each selected child's source. Original / Best Available preserves the most suitable available/native result; final containers and codecs may differ between children.

Explicit conversion applies to new single and collection downloads, and only to the selected children of that new job, never to existing Library media, uploaded files, or files from past jobs. Existing Library bulk conversion/reprocessing is a separate future workflow. Per-child output profiles are out of scope for v0.1; the one chosen output applies to the whole collection.

The application must not imply that increasing a numeric bitrate creates quality absent from the source.

FFmpeg may be used for conversion, remux and other final media processing. Embedded text tags are planned to be written through a thin Mutagen integration instead (§14; planned, not implemented).

M6 decision (approved; planned, not implemented, except the WebM-contained Opus → `.opus` remux, which is implemented in M6.5b/c for single and collection Original audio-only downloads): `Original / Best Available` audio preserves source quality. Opus audio delivered inside a WebM container is remuxed losslessly into an `.opus` container; AAC/M4A is kept as-is. MP3 or AAC re-encoding is not a default behaviour; it happens only when the user explicitly selects a conversion output. Unsupported or unknown source codecs and missing encoders are handled in later technical design, and no encoder is guaranteed on every FFmpeg build. Past Library items are not reprocessed.

Explicit conversion failure (approved; planned, not implemented): when the user explicitly selected a conversion output and that target cannot be produced, the job (or collection child) fails. The application never silently publishes or completes with the original in a different format. This is separate from the optional tag/artwork and `Original` remux fallback in §14.

---

# 9. Video Downloads

Video options must be derived from formats actually available from the source.

Example:

```text
Best Available
1080p
720p
480p
360p
```

If the source maximum is 1080p:

- 1440p must not appear as a genuine quality option.
- 2160p must not appear as a genuine quality option.

**Quality ceilings (approved; planned, not implemented; technical clarification of the example above).** The quality list is a set of ceilings, not exact qualities; the exact-quality reading of the list above is superseded:

- Only the standard ceilings 144/240/360/480/720/1080/1440/2160 are offered, labelled `Up to Xp` (user wording "En fazla Xp"; final UI string is an implementation detail). Selection means the best source format whose real height is `<= X`; there is never upscaling.
- Non-standard heights (for example 608p, 858p, 804p) get no quality row of their own; the actual resolution is shown in the Library.
- If the source maximum is not a standard height, it is shown under the smallest standard ceiling that covers it (608 → `Up to 720p`, 858 → `Up to 1080p`). This never means an exact 720p/1080p result and never upscaling. No ceiling above that top ceiling is offered, and a lower ceiling appears only when a source format with height `<=` that ceiling exists.
- `Best Available` may stay the default. If the height is not reliable, only `Best Available` is offered and no imaginary ceiling is shown. If the source maximum exceeds 2160, `Best Available` may be higher while the ceiling list ends at 2160.
- The smallest-covering-ceiling rule and the `Best Available`-only label without a reliable height apply only to single-item jobs.
- Collection-level ceiling picker (finding 20; user decision, approved, planned, not implemented): collections offer a fixed ceiling list `Up to 2160p` / `1440p` / `1080p` / `720p` / `480p` (plus `Best Available`), without per-item height or probing. For each selected child the backend picks the best source format with height `<= X`; if the child has none, it falls back to that child's best available result, which may then exceed the chosen ceiling. There is never upscaling. The backend already supports `max_height`; the UI does not. This replaces the current behaviour where a child with no format under the ceiling fails as an invalid variant (ARCHITECTURE §19 planned supersede note); the current implementation is unchanged until implemented.

Where video and audio streams are separate, the backend may use `yt-dlp` and FFmpeg to produce the final playable media file.

Collection-level video profiles such as `Best Available` or `Up to 1080p` resolve independently against each selected child's available source formats. A child below the requested ceiling uses an appropriate available quality, never upscaling. A profile is a source-aware preference, not a promise of identical quality, container, or codec across the collection. This source-quality preference is distinct from the output container/codec choice below.

Required first video output set for new downloads (approved; planned, not implemented):

- `Original` (default): source-aware; merges stay in the existing mp4/mkv-style containers chosen for the source.
- `MP4 (compatible)`: the result must be a real H.264/AAC MP4. Compatible streams are copied/remuxed where possible and transcoded only when needed.

Explicit MKV and WebM outputs are outside the first set. Resolution/quality is limited to what the source offers, with no upscaling. For a collection, the one chosen output applies to all selected children, not per child. If an explicit MP4 selection cannot be produced, the job/child fails as described in §8. Transcoding is conversion for compatibility, not quality improvement.

---

# 10. Playlist and Album Support

Playlist and album support is part of v0.1.

After analysis, the user must be able to inspect the discovered items before starting the job.

Example:

```text
Album / Playlist

8 items

☑ 01 — Track One
☑ 02 — Track Two
☑ 03 — Track Three
☑ 04 — Track Four

[ Select All ]
[ Deselect All ]
```

Analyzed, selectable items may be selected by default, subject to the selection limit below.

Users must be able to exclude individual items.

v0.1 has a hard cap of **200 selected items per job**, enforced by the backend as well as the UI. This is not a limit of 200 total items in the source collection.

`Select All` selects only the currently available, analyzed, selectable items, within that cap. It must not silently include unseen, unanalyzed, unavailable, or over-cap items. For large sources the UI must explicitly explain the v0.1 selection limit and the scope of the available selection; it must not imply that the entire source will be downloaded. Analysis retrieval and any pagination strategy remain technical design follow-ups, not requirements established here.

**User decision (planned, not implemented):** the collection analysis shows the first 25 items. Below the list the user can choose `Load 25 more` (+25) or `Show all`, which shows at most 200 items; when the source is longer, a short note says so. While more items load a waiting state is shown, and the user's existing selections are preserved. The 200 selected-item cap above is unchanged. The initial 25 matches the current bounded flat analysis budget (ARCHITECTURE.md §59). No API design exists yet.

Each collection job uses one collection-level Audio / Video mode, source-quality preference and output choice as described in sections 8–9; individual output profiles are not available in v0.1.

The UI should show available metadata before download when possible.

Entire channel/profile bulk downloads are out of scope for v0.1.

---

# 11. Destinations

The download configuration must expose:

```text
Save to

○ App Library
○ Plex Music
○ Plex Videos
○ Download to this device

☐ Keep a copy in App Library
```

## App Library

Stores the physical media inside Downloader-managed storage.

## Plex Music

The final processed media is placed into the mounted Plex Music library.

## Plex Videos

The final processed media is placed into the configured Plex video destination.

## Download to this device

The server prepares the final file.

Once ready, the browser receives it as a standard browser download.

Downloader must not attempt arbitrary direct filesystem writes on the phone/computer.

## Keep a copy in App Library

This option means an actual additional physical copy.

The Library database may reference media stored in Plex without requiring duplication.

---

# 12. Plex Behaviour

Downloader does not need Plex API integration in v0.1.

After a completed media file is moved into the correct Plex directory, Downloader will rely on Plex's normal library scanning behaviour.

No explicit Plex scan trigger is required.

---

# 13. Music Filesystem Organization

Music files must be organized using meaningful artist/album/track names.

Preferred structure:

```text
Music/
└── Artist/
    └── Album/
        ├── 01 - Track Name.ext
        ├── 02 - Track Name.ext
        ├── 03 - Track Name.ext
        └── cover.jpg
```

Example:

```text
Led Zeppelin/
└── Led Zeppelin IV/
    ├── 01 - Black Dog.m4a
    ├── 02 - Rock and Roll.m4a
    ├── 03 - The Battle of Evermore.m4a
    └── 04 - Stairway to Heaven.m4a
```

`cover.jpg` in the tree above is conditional: it appears only when a cover has been selected for that album folder under the artwork rules in §14, and it is not created otherwise.

Generic names such as:

```text
01 Track
02 Track
03 Track
```

must not be used when real titles are known.

M6 decisions (approved; planned, not implemented):

- Artist and album come only from explicit source metadata or user-provided metadata. They are never invented from the title, the channel name is never used as the artist, and a playlist name is not automatically an album.
- Artist folder (approved; planned, not implemented): an explicit album artist takes priority; otherwise the first name of the structured `artists` list; if there is no list, the single source text as-is. A name is never split on `&`, commas or `feat.`.
- Various Artists (approved; planned, not implemented): within one reliable album group, different explicit album-artist values resolve to `Various Artists`. A missing album artist alone is not an inconsistency. Grouping and preparation are the separate M6.6 task; today's child probe only fetches metadata when a child's turn comes. What counts as a reliable group and how a multi-name `album_artists` list becomes a folder value are M6.6 detail decisions and no hypothetical rule is defined here. There is no Library-wide merging by album name and no later moving of files.
- When artist or album is missing, the current flat audio layout is kept and the missing `Artist/Album` folders are not created. No placeholder folder or tag such as `Singles` or `Unknown Artist` is added. When the track name is missing, the source title is used; no automatic title cleanup.
- Audio `NN` numbering (approved; planned, not implemented): a generic audio playlist uses the original source order, and numbers of unselected items are not compacted. Inside a reliable album group with a known track number, `1-01 - Title` is used when the album is known to be multi-disc and `01 - Title` when it is known to be single-disc; an unknown track number is never invented, and when the disc count is unknown multi-disc is not assumed. Playlist source order is never written as the embedded track tag. Video numbering (M6.3) is unchanged. This is a filename rule; it does not by itself decide the embedded track-number tag.
- No automatic title cleanup is applied (for example removing `Official Video`, `Live`, `Remastered`). The name is an explicit source track name or the source title, and the user can edit it manually.
- Edit UI is a technical/UX design follow-up (M6.8).

---

# 14. Music Metadata

Where metadata can be determined reliably, files should contain embedded metadata such as:

- Title
- Artist
- Album Artist
- Album
- Track number
- Total tracks where available
- Disc number where available
- Year/date where available
- Artwork where available and appropriate

Users should be able to inspect or correct important album metadata before the download when automatic metadata is uncertain.

M6.5 text tags (approved; planned, not implemented; M6.5 stays open until the user's real-use Docker Compose smoke result and the full gates): embedded text tags are planned to be written with the existing third-party Mutagen library (not yet an installed repository dependency), not by hand-written format code: MP3 ID3v2.4, M4A MP4 atoms, FLAC / Ogg Vorbis / Opus Vorbis comments. Minimum fields only; there is no genre/composer field and no provider framework.

- `TITLE`: the source track name, otherwise the source title. `ARTIST`: every normalized source name, kept as a natural multi-value tag of the format; the current normalizer limits are not promised to be lifted. `ALBUM` and `ALBUMARTIST`: only explicit source or user data. Track and disc numbers: only known values. Date/year: source release data, never the upload date.
- Total tracks and total discs are not in `SourceMetadata` today and M6.5 does not derive them. Playlist source order is never written as an embedded track tag.
- WAV and the Original WebM fallback (§14 remux fallback) are outside tags and artwork. Video and other unsupported containers are outside this audio-tag scope. An unsupported format is not presented as an error, and no remux or transcode is done just to carry a tag.

M6.7 source artwork (approved; planned, not implemented): by default a track's own thumbnail image is embedded as a square crop; the general playlist image is not used. A missing or broken cover never blocks the download. The user removes or replaces the cover on the M6.8 selection screen. The earlier rule that every source cover requires an explicit selection is superseded and is not an active rule. An album-wide `cover.jpg` is not chosen automatically when tracks have different thumbnail covers; the `cover.jpg` example in §13 is conditional, and the album-wide selection detail stays an open M6.7 design follow-up. For a playlist with different albums, one cover is never applied automatically to all tracks. Format support is not guaranteed.

Alternative cover search (M6.9; approved; planned, not implemented): external search runs only when the user explicitly asks for it on the M6.8 selection screen, never automatically. The UI offers editable artist/album fields and searches an external catalog. Candidates are shown with source/release information and, where verifiable, resolution; the user still chooses the external candidate. High pixel size is not proof of the correct release. There is no silent search, selection, or replacement, and choosing a cover does not change other metadata on its own. A failed search or a missing cover never blocks the download. This is the last separate M6 sub-task and is not started: provider and rights terms must be clarified when M6.9 integration is reached; no new rights research or provider decision is made now. It is neither deferred nor completed automatically.

Order of work: M6.5 text tags → M6.6 grouping decision and paths → M6.7 source artwork → M6.8 selection/editing → M6.9 user-triggered external search.

Library note: Mutagen is GPL-2.0-or-later. That is not a blocker for personal use; a distribution/project-license compatibility note remains open, and this is not a legal approval.

Provider and rights status: MusicBrainz search plus Cover Art Archive is only the first provider candidate, as a single chain rather than two independent catalogs. It is not a final provider choice and no integration is complete. No new provider framework, plugin system or separate service is planned. The application will be distributed as open source; code, data and image licenses are separate, open source does not automatically mean non-commercial, and covers are not bundled with the software package. API access or a user's selection does not replace copyright permission, and rights for external preview/cache/embed are not yet verified. The research is not full legal approval. Official terms: https://musicbrainz.org/doc/MusicBrainz_API , https://musicbrainz.org/doc/About/Data_License , https://musicbrainz.org/doc/Cover_Art_Archive , https://metabrainz.org/supporters/account-type .

Tag or artwork processing failure (approved; planned, not implemented): tagging runs on a separate staged candidate copy and has two branches. (1) If tagging and the re-read verification succeed, the verified candidate is published through the normal storage containment, conflict and catalog path. (2) If tagging fails, the failed candidate is never published; the sound prepared output from before tagging is kept intact and published through the same normal storage containment, conflict and catalog checks, and the job completes with an Activity warning. A ready explicit MP3/M4A target never reverts to the old pre-conversion source format. Cancellation, security, storage and catalog errors are not swallowed by this fallback. `Original` audio remux failure is not covered here; it is governed only by the separate paragraph below. This fallback never replaces an explicitly selected conversion (§8–9): if the selected target output is ready and only optional tagging/artwork fails, the target is kept and the pre-conversion source format is not restored; if the selected target cannot be produced, the job/child fails.

`Original` audio remux fallback (approved; implemented in M6.5b/c for single and collection downloads): this is a separate rule from the tag/artwork rule above and does not narrow or widen it. When Original audio is WebM-contained Opus and the lossless `.opus` remux is attempted, the safe original WebM file is published with an Activity warning only if FFmpeg actually ran, failed with a non-zero exit, and the job was not cancelled. The file is still published through the normal storage containment, conflict and catalog checks; if any of those fail, the job/child fails and the safe original is not published without them. This is a narrow, user-accepted exception: a disk or permission error inside the FFmpeg process may fall under this general warning, so there is no guarantee that storage errors are always reported separately. Cancellation and direct security, storage and catalog errors are never swallowed. FFmpeg not being found, a timeout, a failed target safety check, a failure to start FFmpeg, a missing, empty or unverifiable output, and any unclassified failure are not covered by the fallback; the job/child fails. If the explicitly selected output cannot be produced, the job/child fails; there is no silent fallback to another format or source, and no UI to change the format or retry with a different choice. This rule is not limited to single downloads; the order in which single and collection downloads implement it is not part of this specification.

Downloader must avoid confidently inventing metadata from ambiguous titles.

---

# 15. Video Filename Rules

Standalone video:

```text
Title.ext
```

Video inside a playlist:

```text
01 - Title.ext
02 - Title.ext
03 - Title.ext
```

In a playlist, `NN` follows the original source playlist order; numbers of unselected items are not compacted (M6.3, implemented for collection video files: the persisted `source_order` with at least two digits and no compaction; standalone video and audio filenames are unchanged; audio numbering is approved but planned, see §13, and does not change video numbering). Automatic title cleanup is not applied; the title is the source title, which the user can edit (manual name editing UI planned, not implemented).

Technical IDs such as a YouTube video ID should not be included in the visible filename by default.

Source IDs and URLs should remain available in application metadata/database records.

---

# 16. Filename Conflicts

Default behaviour:

`Skip Existing`

Additional options:

- Replace
- Keep Both

The application must never silently overwrite an existing media file.

**Video naming and collision results (approved; planned, not implemented).** A video file name carries the actual resolution of the produced file: `Title (720p).mp4`. Any other name collision uses `(2)`. When the request is exactly the same as an existing one, the existing file is kept and the job is shown as `Already exists`, not `Completed`. This is a user-visible result decision only: no new database state enum and no new API shape has been chosen yet. Planned supersede notes for deprecated behaviour: showing a differently-qualified request as successfully completed from an existing path (finding 3) is superseded by these rules. Note that distinct logical Library rows sharing one physical file are not data corruption: the current skip-existing test contract is deliberate and stays until a storage/catalog contract task decides otherwise; presenting the wrong quality as a successful result from an existing path is a separate defect.

**Replace / Keep Both choice surface (open; user-transferred spec audit).** Where the user chooses Replace or Keep Both (per job, default in Settings, or both) is undecided. The proposed M9-C design task (ARCHITECTURE §59) covers only the Replace / Keep Both behaviours and this choice surface. The correctness of the approved resolution suffix, `(2)` and `Already exists` rules above stays with the finding 3 expert task, after Queue Q1/Q2 and before Q3 and M6.5 in the approved order; M9-C must preserve and align with that contract. There is no joint delivery, and that pre-M6.5 work does not depend on M9. No conflict primitive is implemented by this record.

---

# 17. Queue

Queue management is required in v0.1.

Required job states:

- queued
- analyzing
- downloading
- post-processing
- completed
- failed
- cancelled

Required actions where appropriate:

- Cancel
- Retry
- ~~Remove~~ and ~~Clear Completed~~ — superseded (approved) by the Clear History decision below; there is no per-job remove.

**Queue page status and decisions.** Observed: the Queue page is still a placeholder. It is a v0.1 required capability, and its prerequisite M6-Q comes before M6.5 tag work (ARCHITECTURE §59, which holds the technical details). All items below are approved decisions, **planned, not implemented**, unless marked otherwise:

- The page lists active, queued and history jobs with Cancel and Retry (parent-only for collections, per-child UI stays out of scope).
- **Clear History** replaces the old Remove / Clear Completed actions. There is no single history/job deletion and no per-job delete endpoint (user-settled). A bulk confirmation screen shows the number of jobs and the space that would be freed. By default completed and failed jobs are selected; cancelled jobs (single and collection) stay. An `include_cancelled` checkbox is OFF by default; when checked, the cancelled jobs' partial staging data (including a cancelled single job's retained partial staging) is deleted too. Library media files and catalog records are never deleted by this cleanup. Active and queued jobs are never cleanup candidates.
- **Failed staging (approved):** Clear History also deletes the staging data of the failed jobs it clears. There is no automatic staging deletion at the moment of failure.
- **Cancelled single Retry (approved):** a cancelled single job keeps its partial staging and can be retried, continuing from where it stopped. Resumption happens only through an explicit user Retry, never automatically. This is a separate bounded Opus expert task in Queue Q2 (ARCHITECTURE §59).
- **Still open, awaiting explicit user approval:** the byte estimate versus the actually freed amount and real reporting of cleanup failures.
- The clear endpoint does not exist yet (the bulk backend is built from scratch in Q3); delivery is staged and no API payload is designed here (ARCHITECTURE §59).
- Current behaviour (observed, not yet changed): a cancelled single job cannot be retried and its partial data is removed; a cancelled collection can be retried and resumes. The approved cancelled single Retry above changes this only when Q2 is implemented.

**Progress display (approved; planned, not implemented).** Conversion shows its real progress percentage; when it is unknown the bar is indeterminate. Source transfer at 100% is not conversion at 100%, and no smoothing is promised. Observed, unfixed (finding 7): during conversion the UI showed 100% for minutes while ffmpeg ran at about 1000% CPU.

Example:

```text
ACTIVE

Led Zeppelin IV
4 / 8 tracks
61%
8.1 MiB/s

QUEUED

Video ABC
Playlist XYZ

FAILED

Video DEF
[ Retry ]
```

The architecture should support a configurable concurrency limit.

A conservative default concurrency value may be used initially.

For playlist/album jobs, preserve the real completed, failed, and total selected-item counters. A terminal mixed result with some completed children and at least one failed child has parent state `failed`; there is no new partial-success state. Completed children and their outputs remain valid and must not be repeated. Child-specific failure reasons remain inspectable; a parent `partial_failure` error code may summarize this mixed result but is not a job state.

Parent Cancel cancels active child work and prevents not-started children from running, without touching completed children. Parent Retry reruns only failed, cancelled, or incomplete children; completed children are not downloaded again. Per-child Cancel / Retry UI is out of scope for v0.1. When all selected children have failed, the parent is `failed` with stable `all_children_failed` error semantics: that parent error code is stable whether the child failure reasons are homogeneous or heterogeneous, child-specific reasons and error counters remain inspectable, and neither the first/last child error nor `partial_failure` is substituted. Parent Retry is enabled only when at least one retry-eligible failed, cancelled, or incomplete child exists; the locked child retry-eligibility classification is recorded below; exact error message wording, schema migrations and new public API surface remain outside that classification. When parent cancellation races with child failure, precedence is decided by durable state only: if the Cancel request is persisted before the parent's terminalization transaction, the parent is `cancelled`; if a failure terminal was committed first, the parent remains `failed` and a later Cancel cannot supersede it. In-memory event timing is never the decision source, and completed children and their outputs remain immutable. Shutdown policy and durability/data-schema choices remain outside this product paragraph.

M5 implementation status: collection dispatch, durable Parent Cancel, Retry, startup recovery and
orderly shutdown settlement are implemented. The contracts below remain authoritative; M5 passed
independent milestone review and fresh full quality gates and is accepted.

Locked parent Cancel lifecycle decisions (implemented contract): after a durable Parent Cancel acceptance and the full attempt-owned drain, queued or active children (the active states analyzing, downloading, post_processing, finalizing) become `cancelled` in one atomic settlement; existing failed child states and reasons and the failed counter remain real, completed children, their outputs and catalog entries stay immutable, and the settlement derives the actual total/completed/failed N/C/F with no new cancelled counter. The all-completed precedence is expressly confined to all selected children completed: when every selected child's completion is already durably committed (a nonempty all-completed selected set) and the parent success terminal is not yet committed, a committing Cancel marker settles the parent as `completed`, not `cancelled` — children win — while a success terminal committed first rejects a late Cancel. This decision is not an extension of the narrow F >= 1 failure-race precedence, which stays locked: a Cancel request persisted first yields `cancelled`, a failure terminal committed first leaves the parent `failed`. NULL-marker child claim/forward/progress/completion predicates remain unchanged, no completion or catalog insert is permitted after the marker, and the marker is not cleared on success. This paragraph defines no helper design, new API, message wording, retry eligibility, or error reset.

**Locked collection Retry eligibility (implemented contract).** User-locked product decisions implemented by the collection Retry lifecycle: all cancelled children are retry-eligible regardless of their persisted reason or error code. Parent Retry is permitted only on a terminal failed or cancelled collection parent; completed, queued/active and unknown/corrupt parents reject Retry. After the full old-attempt drain, old-attempt children in queued, analyzing, downloading, post_processing, or finalizing count as incomplete and are retry-eligible, and the queued state is prepared inside the same successful atomic retry/reset, not persisted as a new status. For failed children, a known persisted error code stays authoritative against the existing `job_error_code_retryable` list, which is not broadened; a missing or unknown persisted code is ineligible, and transient `JobError.retryable` never overrides the persisted code classification, so no new persisted override is introduced. Parent retry eligibility is the existence of at least one retry-eligible child, never the aggregate parent error code. Completed children and their outputs stay immutable and are never rerun, even after Library deletion; the retry settlement preserves the actual N/C counters with no new cancelled counter, and there is no requirement to reset all ineligible children or reclassify unknown/corrupt entries. This paragraph defines no API, migration, recovery, or shutdown design; recovery normalization and shutdown remain outside this contract.

**Locked collection startup recovery precedence (implemented contract).** User-locked product decision implemented by collection startup recovery: when startup recovery finds a previously active locked collection parent whose full nonempty actual selected child set is already committed with statuses only completed or failed, actual settled child facts win before interruption normalization — all completed (C = N, F = 0) settles the parent `completed`; F = N settles it `failed` with stable `all_children_failed`; C > 0 with F > 0 and C + F = N settles it `failed` with `partial_failure`. This precedence is evaluated before the active→interrupted failed-child normalization and is not generated by it; cached parent counters and event timing are never decision inputs. Existing completed catalog entries and outputs and persisted failed child reasons stay immutable, and the settlement uses the actual N/C/F with no synthesized counts or fabricated partial failure. Recovery decisions are applied before new child claims; an already-terminal parent is unchanged, and a normal queued parent with a NULL marker remains queued. When a persisted Cancel marker exists on a queued/active parent, the locked Parent Cancel matrix applies unchanged (pending/queued/active children become `cancelled`, failed/completed children stay immutable, all-completed children win) and the marker is never cleared at startup. Without a marker, the existing interruption normalization stands: the interrupted active child becomes `failed` with `interrupted_by_restart`, queued children are unchanged, existing failed/completed children are preserved, and the parent becomes `failed` with `interrupted_by_restart` and real atomic N/C/F (see ARCHITECTURE §21). This paragraph records only the settled child facts precedence for user-selected locked collections; cancelled children under a NULL marker, unknown/empty/corrupt sets, terminal, durability and state handling, and auto-resume/Retry/shutdown policy stay unsupported and separately scoped.

**Locked collection orderly shutdown settlement (implemented contract).** User-locked product decision recorded now with
ARCHITECTURE §19 and the §21 note below: on a normal orderly shutdown, `queue_manager.stop()` stops new
claims, awaits the full drain of collection attempts and ongoing API Cancel/Retry operations (including
probes, downloader/merge processes, storage-gate helpers, callbacks, owned transactions and session
close), and then commits collection durable settlement using the already locked startup recovery matrix
before it returns; only after `queue_manager.stop()` returns does `engine.dispose()` close the database. No
new recovery matrix is introduced. The settlement applies the locked branches as-is: a nonempty selected
child set already committed with statuses only completed or failed settles from actual facts with normal
precedence before interruption normalization (all completed (C = N, F = 0) → `completed`; F = N →
`failed`/`all_children_failed`; C > 0 with F > 0 → `failed`/`partial_failure`); a pending actual active child
becomes `failed`/`interrupted_by_restart` and the parent `failed`/`interrupted_by_restart` with real atomic
N/C/F; queued siblings are unchanged; and a committed Cancel marker keeps the locked Cancel matrix authoritative
with the marker never cleared. No callback or runtime event timing is a decision input, and no synthetic
User Cancel marker is produced. Existing durably terminal parents stay unchanged, and completed outputs,
catalog entries and persisted failed reasons are preserved. Unclaimed queued jobs are not interrupted
merely by a shutdown: a queued parent without a marker remains queued, and an already committed Cancel
obeys the existing Cancel contract — no ignore-marker behaviour is invented. The full owned drain is
mandatory and technical, recorded per the existing pattern: the database stays open until settlement operations
finish, and the SQL writer and lifecycle locks are held outside process/helper waits to avoid a stop-phase
deadlock; no new coordinator API or implementation design is introduced. Existing single-job shutdown is
unchanged; this is a collection-only decision. Not chosen and unsupported: unknown/corrupt/empty child
sets, cancelled children under a NULL marker, any new status/API/migration, auto-resume or Retry on shutdown,
and any new durability guarantee for SIGKILL or unbounded force-kill behaviour. If the orderly shutdown
cannot finish or commit its settlement, no completed state is claimed and the existing full startup recovery
applies at the next startup — this adds no timeout policy. Collection orderly shutdown settlement and
production dispatch are implemented and accepted with M5.

---

# 18. Activity Panel

Every active or completed job should expose a collapsible Activity panel.

Collapsed by default:

```text
Activity ▸
```

Expanded:

```text
Activity ▾

14:42:11 Analyzing URL
14:42:12 YouTube detected
14:42:13 8 items found
14:42:15 Download started
14:42:28 Post-processing
14:42:31 Saved to Plex Music
```

The v0.1 panel should show clean, structured, human-readable activity events.

It should not simply dump unrestricted process output directly into the UI.

Sensitive information must not be exposed through logs.

Possible transport mechanisms such as WebSocket or SSE will be determined in the technical architecture.

---

# 19. Advanced Logs — NOT v0.1

Future versions may extend Activity into:

```text
[ Activity ▼ ]

○ Simple
● Detailed
```

Detailed mode may expose sanitized `yt-dlp` and FFmpeg information.

Possible future capabilities:

- INFO/WARNING/ERROR filtering
- raw-ish logs
- Copy Log
- Download Log
- debug information

This must not block the v0.1 release.

---

# 20. Library

Downloader must contain a Library page.

The Library represents downloads known to the application whether their physical location is:

- App Library
- Plex Music
- Plex Videos

Example columns:

```text
Name
Type
Size
Downloaded At
Format
Location
Path
```

Example:

```text
Led Zeppelin IV
Album
386 MB
27 Sep 2026 14:31
M4A
Plex Music
/media/music/Led Zeppelin/Led Zeppelin IV/
```

Selecting a Library item should reveal additional information such as:

- title,
- thumbnail/artwork,
- original URL,
- platform,
- extractor/source,
- download date,
- final file size,
- audio/video format,
- bitrate/resolution where relevant,
- physical path,
- playlist/album membership.

Observed gap (user-transferred spec audit, not re-verified here): the Library detail does not yet show thumbnail/artwork, extractor/source and playlist/album membership. Membership depends on the M6.6 grouping decision; the proposed M9-L task tracks it (ARCHITECTURE §59). The requirement above is unchanged.

---

# 21. Library Deletion Model

Two distinct operations are required (their UI is the single Delete modal described at the end of this section).

## Remove from Library

Deletes the application's database/library record.

The physical media file remains untouched.

## Delete File

Deletes the actual media file from disk.

The target is one physical copy, selected through its Library entry, never a client-supplied filesystem path.

If another catalog entry references the same physical file path, the file is not deleted: the operation is blocked and the reason is reported. The file may be deleted only when the selected copy is the only reference; the physical file and that copy's catalog record are then deleted together, and deleting the last copy also removes the Library item.

This action must require explicit confirmation.

The interface must make the difference between these operations obvious.

**Single Delete modal (approved; planned, not implemented).** This replaces the earlier requirement of two separate UI actions, and `Remove from Library` is no longer unconfirmed. One Radarr-style Delete modal is used, with `Also delete files` OFF by default:

- Unchecked: only the catalog record is removed; the physical file stays.
- Checked: the existing safe selected-copy delete runs. When M7 introduces several copies, the user chooses per copy. There is no arbitrary path, and the shared-reference deletion guard is not removed.
- The new modal is not implemented now; the existing backend remove/delete primitives may remain separate.
- Finding 4: the shared-delete error message must state clearly that the catalog references must be removed first; this explains the guard and is not a bypass.

---

# 22. Main Screens

v0.1 should contain approximately the following primary sections:

```text
New Download
Queue
Library
Settings
```

Placeholder pages (approved; planned, not implemented): menu entries for pages that are still placeholders stay visible with a "not built yet" label (user wording "Henüz yapılmadı"; final UI string is an implementation detail). The UI is not changed by this record; timing is in ARCHITECTURE §59 M6-Q.

A separate Platforms screen is not required unless later justified.

Platform selection belongs primarily to New Download.

---

# 23. Settings

Initial settings may include:

- App Library filesystem path
- Plex Music path
- Plex Videos path
- maximum concurrent jobs
- default conflict behaviour
- default download preferences where appropriate

Advanced settings should remain limited in v0.1.

Current status (observed; user-transferred spec audit): the Settings page is an 8-line placeholder and configuration is environment-only; there is no settings endpoint, model or table. The list above is the long-term direction; v0.1 scope is narrowed by the approved plan below.

**Settings plan (approved user decision; planned, not implemented; ARCHITECTURE §47, §59):**

- v0.1 delivers a read-only Settings page (S1) at the M7 opening, validated against the M7 target: it shows the effective Compose/environment values, which destinations are configured, and the stored startup write-check result. Viewing Settings never runs a new check and changes nothing.
- At startup the application test-writes and deletes a small file in each configured target and staging folder. Success is silent; a failure shows a persistent banner. The result reflects permissions at that moment only and does not guarantee future writes.
- Editable, saved settings (S2) are post-v0.1 (§33). Until then settings are changed through Compose/environment, and in-app defaults are fixed.
- An optional welcome screen is post-v0.1.

---

# 24. Authentication

v0.1:

- No application login.
- Designed for trusted private network access, primarily Tailscale.

Architectural requirement:

Authentication must be reasonably addable later without rewriting the entire product.

Business/domain logic should not assume forever that only one unauthenticated global user can exist.

Do not build a full unused authentication system in v0.1.

---

# 25. Cookie / Authenticated Source Support

Browser cookies, account sessions, and authenticated media-source workflows are NOT part of v0.1.

If media requires authentication, the application should return an appropriate user-facing error.

Cookie/session support is explicitly reserved for a later iteration.

---

# 26. Persistence

Application state must survive container restarts.

Persistent data includes at minimum:

- Library database
- queued/completed job records where relevant
- application settings
- App Library media
- logs/activity history where retained
- download state needed for reasonable recovery

SQLite is sufficient for v0.1.

---

# 27. Staging

Downloads should not be written directly into Plex's final library location while incomplete.

Recommended workflow:

```text
yt-dlp
   ↓
staging/
   ↓
FFmpeg / metadata / artwork / rename
   ↓
successful completion
   ↓
final destination
```

Incomplete `.part`, temporary, or failed files should remain outside the Plex library.

---

# 28. Technology Direction

Expected v0.1 stack:

## Backend

- Python
- FastAPI
- yt-dlp
- FFmpeg
- SQLite

## Frontend

- React
- TypeScript

A lightweight modern frontend build system may be used.

## Deployment

- Docker
- Docker Compose
- persistent bind mounts / volumes
- Tailscale/private reverse proxy environment

The detailed technical architecture will be documented separately in `ARCHITECTURE.md`.

---

# 29. Repository Direction

Expected monorepo structure:

```text
downloader/
├── PRODUCT_SPEC.md
├── ARCHITECTURE.md
├── AGENTS.md
├── backend/
├── frontend/
├── tests/
├── compose.yml
└── README.md
```

This structure may be adjusted by the architecture phase if there is a clear technical reason.

---

# 30. First Vertical Slice

The first implementation milestone should NOT attempt to implement the entire product.

The first complete vertical slice should support:

```text
Paste URL
    ↓
Analyze with yt-dlp
    ↓
Display detected source
    ↓
Display real available formats
    ↓
Choose one format
    ↓
Download
    ↓
Display progress/activity
    ↓
Save to App Library
    ↓
Display result in Library
```

This slice must work end-to-end before adding the more complex destination and playlist functionality.

---

# 31. v0.1 Required Capabilities

The v0.1 release target includes:

- Dockerized web application
- responsive web interface
- mobile-friendly operation
- Tailscale/private-network usage
- platform dropdown
- Auto Select default
- manual platform override
- URL analysis
- yt-dlp-compatible sources
- extractor failure messages
- audio/video selection
- source-derived quality options
- no fake upscaling
- single-item downloads
- playlist/album support
- playlist item selection
- FFmpeg post-processing
- App Library destination
- Plex Music destination
- Plex Videos destination
- Download to this device
- optional Keep a Copy in App Library
- queue (a real Queue page with active/queued/history, Cancel/Retry and Clear History; §17, planned in M6-Q)
- progress
- cancel
- retry
- failure states
- basic Activity panel
- SQLite-backed Library/history
- physical file size
- download date
- path information
- meaningful music naming
- meaningful video naming
- media metadata
- conflict handling
- Remove from Library (single Delete modal, `Also delete files` OFF by default; §21, approved, planned)
- Delete File with confirmation (same modal, checkbox ON; §21)
- persistent application state

---

# 32. Explicitly Out of Scope for v0.1

Do not implement these unless the product specification is deliberately revised:

- application user accounts
- login screen
- public internet deployment
- cookies / authenticated-source integration
- automatic extractor health checks
- extractor uptime/history dashboard
- Simple/Detailed advanced Activity modes
- unrestricted raw process logs
- Plex API integration
- explicit Plex scan triggering
- full channel/profile downloads
- per-item output profiles for collection jobs
- per-child Cancel / Retry UI
- existing Library bulk conversion/reprocessing
- scheduled downloads
- subscription synchronization
- Watch Later synchronization
- browser extension
- native iOS application
- native Android application
- Redis
- Celery
- Kubernetes
- artificial media upscaling

---

# 33. Future Backlog

These ideas are intentionally preserved for later iterations:

## Authentication
Optional login and multi-user support.

## Authenticated media sources
Cookie/session support for sources requiring authenticated access.

## Advanced Activity
Simple/Detailed/raw-ish logging modes.

## PWA / Share Target
Allow mobile users to use:

```text
YouTube
→ Share
→ Downloader
```

and automatically populate the URL.

## Browser Extension
Quickly send the current page/media URL to Downloader.

## Plex API Integration
Possible explicit library refresh/scan.

## Large Collection Workflows
Channels, profiles, subscriptions, and other potentially very large collections.

## Existing Library Conversion / Reprocessing
Bulk conversion or reprocessing of already downloaded Library media, file-upload conversion and per-child output profiles are separate from new-job conversion and remain future work.

## Scheduled Downloads
Run defined download jobs automatically.

## Advanced Queue Infrastructure
Redis/Celery or another distributed job system only if actual scale/reliability needs justify it.

Do not introduce this infrastructure preemptively.

## Editable Settings (post-v0.1; approved deferral)
Saved, in-app editable settings (former M9 S2a/S2b/S2c: database-backed settings, validated changes including concurrency, and Settings UI defaults), plus an optional welcome screen. v0.1 uses Compose/environment configuration with a read-only Settings page (§23).

## Theme selector (optional; not v0.1 required)
A Light / Dark / System theme choice remembered in the browser may be added later. The app already follows the system light/dark preference; a manual choice is optional polish, scheduled only with user approval, otherwise post-v0.1.

## Clip downloads and subtitles (post-v0.1 proposal; not v0.1 required)
Suggestions only, not approved scope and not v0.1 required: clip download through yt-dlp `download_ranges` with a two-handle slider plus time boxes; subtitles as none / embed / sidecar with a language choice.

## Authorized DRM Licensing / Decryption Integration
Post-v0.1 research/evaluation item only. Integration with an official, provider-supported DRM licensing/decryption path would be considered only for content the user is validly licensed to access and explicitly permitted to save/download, and only after upfront technical, legal, and provider-compatibility feasibility evaluation. No supported-provider guarantee, provider list, or implementation design is committed here. This must not become DRM circumvention, key extraction, or protection bypass; the existing no-circumvention principle remains.

---

# 34. UX Requirements

The interface should be:

- responsive,
- usable from a phone,
- understandable without `yt-dlp` knowledge,
- clear about current job state,
- conservative about destructive actions,
- explicit about errors,
- transparent about conversion versus genuine source quality.

Users should normally be able to complete a download without seeing a terminal.

The terminal-like Activity view is optional supporting information, not the primary interface.

---

# 35. Acceptance Criteria for v0.1

The product is considered functionally ready for v0.1 when a user can:

1. Open Downloader from a phone or desktop browser over the private network.
2. Leave Platform set to Auto Select.
3. Paste a supported media URL.
4. Analyze the URL successfully.
5. See the detected media and source information.
6. Choose from valid source-derived download options.
7. Add the job to the queue.
8. Observe meaningful download progress.
9. Expand Activity and understand what the job is doing.
10. Complete a download successfully.
11. Save media into App Library or configured Plex destinations.
12. Download the prepared media to the current browser/device.
13. Download a selected subset of a playlist/album.
14. View the completed media inside Library.
15. Inspect its size, date, location, format, and path.
16. Retry a failed job.
17. Cancel an active job.
18. Remove an item from Library without deleting its file (Delete modal, checkbox OFF; §21).
19. Explicitly delete a physical file after confirmation (Delete modal, checkbox ON; §21).
20. Restart the Docker application without losing persistent Library state.

Release gate: this functional readiness is complemented by a one-time v0.1 Release Security Hardening / Audit stage (ARCHITECTURE section 59), run after Milestone 9 and before final v0.1 release acceptance; unresolved validated Critical/Important security findings block the release.

---

# 36. Development Rule

Do not attempt to implement all features in one agent task.

Implementation should proceed as small, testable milestones.

Each milestone should:

1. have clearly defined scope,
2. preserve existing working behaviour,
3. include relevant tests,
4. pass lint/type/build checks,
5. be reviewed before starting the next major capability.

The specification is the product source of truth.

If implementation decisions conflict with this document, either:

- change the implementation, or
- explicitly update this specification after deciding that the product requirement itself should change.
