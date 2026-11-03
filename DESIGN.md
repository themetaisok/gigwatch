# gigwatch DESIGN

Personal gig alerts for one user in Barcelona. Python 3.11+ (stdlib, requests, pyyaml, feedparser, python-dateutil, rapidfuzz). Tests use pytest and `responses`. Runs every 3 hours as a GitHub Actions cron in private repo `themetaisok/gigwatch`, and locally via `python -m gigwatch run --config config/`.

## 1. Layout
```
gigwatch/{__main__,cli,config,models,normalize,geo,match,dedupe,state,notify_telegram,render,spotify_import}.py
gigwatch/sources/{base,ticketmaster,bcn_opendata,feeds,inbox,bandsintown}.py
config/{settings,artists,venues,genres,promoters}.yaml   tests/fixtures/   docs/{COVERAGE,SETUP}.md
state/state.json   requirements.txt   README.md   .gitignore (.env .venv cache/ __pycache__)   .github/workflows/{gigwatch,pages}.yml
```

## 2. Models (`models.py`)
```python
@dataclass
class Event:
    uid: str; source: str; source_ids: set[str]; title: str; artists: list[str]
    venue: str|None; city: str|None; lat: float|None; lon: float|None
    start: datetime|None   # tz-aware Europe/Madrid; None only for heads-up
    end: datetime|None; url: str|None; status: str  # onsale|offsale|canceled|postponed|rescheduled|unknown
    genres: list[str]; raw_tier_hint: str|None  # must-see|maybe|heads-up
    fetched_at: datetime; onsale_at: datetime|None = None
    tier: str|None = None; sources_urls: dict[str,str] = field(default_factory=dict)
    time_unknown: bool = False; description: str = ""
```
`Source` (base.py): `name: str; priority: int; enabled(env)->bool; fetch(cfg, now)->list[Event]`. Raises on total failure; the runner catches it and logs.

## 3. Config
- `settings.yaml`: `home{lat:41.3874,lon:2.1686,radius_km:150}`, `timezone: Europe/Madrid`, `horizon_days:180`, `region_keywords:[...]` (a user-extensible `extra_region_keywords` is merged in), `maybe_digest{weekday:1,hour_local:9}` (Monday=1, ISO), `min_lead_days_warning:2`, `quiet:false`, `max_immediate:15`, `feeds:[{name,url,tier_default:maybe}]`, `source_priority:[ticketmaster,bcn_opendata,bandsintown,inbox,feeds]`.
- `artists.yaml`: list of `{name, aliases:[], tier: must-see|maybe (default must-see), exclude:[], spotify_id?, notes?}`.
- `venues.yaml`: `{name, aliases, city, lat?, lon?, tier: maybe}`.
- `genres.yaml`: `{ticketmaster_genres: []}` (empty by default).
- `promoters.yaml`: `{name, aliases}` (tier maybe).
- Region keywords seed: Barcelona, Badalona, L'Hospitalet, Hospitalet, Sitges, Girona, Tarragona, Lleida, Mataró, Terrassa, Sabadell, Vilanova, Reus, Salou, Montmeló, Cornellà, Sant Cugat, Granollers, Santa Coloma, Castelldefels, Viladecans, Gavà, El Prat, Sant Adrià, Catalunya, Cataluña, Catalonia.
- Seed artists (must-see): Peggy Gou, Dixon (exclude: dixon brothers), Âme, Solomun, Honey Dijon, Jamie xx, Four Tet, Floating Points, Bicep, Overmono, Fred again.., Caribou, Ben UFO, Helena Hauff, Nina Kraviz, Amelie Lens, Charlotte de Witte, Marcel Dettmann, DJ Koze, Ricardo Villalobos, Maceo Plex, Paul Kalkbrenner, John Talabot, Fontaines D.C., IDLES, Khruangbin, Mogwai, Arca, Jungle, Róisín Murphy, Hot Chip, Jamie Jones, Seth Troxler, Black Coffee, Keinemusik, Adam Beyer, Richie Hawtin, Carl Cox. Venues (maybe): Nitsa / Sala Apolo / La [2] de Apolo, Razzmatazz (+ The Loft, Lolita, Pop Bar), Input, Moog, Laut, Macarena Club, Sala Upload, Paral·lel 62, La Paloma, Sidecar, Sala Bikini, Sala Wolf, Jamboree, Sant Jordi Club, Palau Sant Jordi, Parc del Fòrum, Poble Espanyol, Pacha Barcelona, Sutton, Opium, Marula Café, Sala Vol, Fabra i Coats. Promoters: Brunch Electronik, Brunch in the Park, Primavera Sound, Sónar, Sónar by Night, Off Week, Cruïlla, Mira Festival, Elrow, Barcelona Beach Festival, Share Festival.

Validation in `config.load(dir)->Config`. Unknown keys warn. A missing file is treated as empty, except settings, which is required.

## 4. normalize.py / geo.py
`norm(s)->str`: NFKD, drop combining marks, casefold, `&`→`and`, remove `.` and `'`, other punctuation→space, collapse whitespace. "Fred again.." → "fred again"; "Âme" → "ame".
`geohash(lat,lon,precision=5)->str`: pure Python base32 encoder.
`haversine_km(a_lat,a_lon,b_lat,b_lon)->float`.
`in_region(ev,cfg)->bool`:
1. If lat/lon are present, return distance <= radius_km. Never fall back to keywords.
2. Otherwise, return True if any norm(region keyword) is a whole-word match in norm(city + " " + venue).
3. Otherwise, False.
`distance_km(ev,cfg)->float|None`. Show "· N km" when distance is above 15 km.

## 5. Matching (`match.py`)
`match_artist(ev,artists)->list[ArtistHit]`. For each artist, build the alias set {name}+aliases, normalized. Candidates are ev.artists (attractions/lineup) and the title.
1. Exact: a normalized alias equals a normalized candidate artist name.
2. Fuzzy (only when len(alias)>=5): `rapidfuzz.fuzz.token_set_ratio(alias,cand)>=92`, and the shorter side must have >=5 chars. Applies to artist candidates only, never to the title. Because `token_set_ratio` scores 100 on subset matches ("dixon" vs "dixon brothers"), fuzzy additionally requires `len(tokens_alias)==len(tokens_cand)`.
3. Title: whole-word regex `(?<!\w)alias(?!\w)` on the normalized title, only if len(alias)>3. Aliases of <=3 chars (e.g. "ame") match only on step 1.
4. `exclude` phrases on an artist void any match for that artist when the normalized phrase appears in the title or any candidate.

`assign_tier(ev,cfg)->str|None`, in order:
1. must-see: any hit on an artist with tier must-see.
2. maybe: hit on a tier=maybe artist, a venue match, a promoter match (whole-word on title and description), or a genre match (TM genre or subGenre in genres.yaml).
3. Otherwise, a feed/inbox item with raw_tier_hint heads-up.
4. Otherwise None (the event is dropped).

Venue match: norm(ev.venue) equals a venue alias, or `fuzz.ratio>=90`, or a venue alias (len>=5) appears as whole words inside norm(ev.venue).

## 6. Sources
**Ticketmaster.** `fetch` runs mode A (geo) and mode B (one query per followed artist; must-see and maybe both). Mode A params: apikey, geoPoint=geohash(home,5), radius, unit=km, countryCode=ES, classificationName=Music, size=200, page=N from 0, sort=date,asc, locale=*, startDateTime=now formatted `%Y-%m-%dT%H:%M:%SZ`, endDateTime=now+horizon. Mode B: keyword=<artist>, countryCode=ES, classificationName=Music, size=50, same dates. Stop when `_embedded` is missing, `page.number+1>=page.totalPages`, or `(page+1)*size>=1000`. Throttle to <=4 req/s (sleep 0.25s). In-memory per-run cache keyed by full param tuple. Retry 429/5xx x3. Map: id→source_ids `tm:<id>`; start = `dates.start.dateTime` (UTC→Madrid), else localDate+localTime (localTime may be missing or malformed; fall back to 00:00 and mark `time_unknown`); status from `dates.status.code`; attractions→artists; venue from `_embedded.venues[0]`, with lat/lon parsed from strings when present; genres from classifications (segment, genre, subGenre names); `onsale_at` from `sales.public.startDateTime`; url from `url`. Budget: ~(40 artists + ~5 pages) per run, 8 runs/day, well under 5000.

**BCN Open Data.** `resolve_csv_url()` calls CKAN `https://opendata-ajuntament.barcelona.cat/data/api/3/action/package_show?id=agenda-cultural` and picks the first resource where `format.upper()=="CSV"`. On any failure it uses the fallback constant URL `https://opendata-ajuntament.barcelona.cat/data/dataset/2767159c-1c98-46b8-a686-2b25b40cb053/resource/3abb2414-1ee0-446e-9c25-380e938adb73/download`. The file is updated daily, about 10 MB, **UTF-16 LE with BOM**, comma delimited, quoted fields. `decode_bytes` uses `utf-16` when a BOM is present (it honours the BOM), then utf-8-sig, then latin-1.

The real header (40 columns) is: register_id, name, institution_id, institution_name, created, modified, addresses_*(roadtype, road, start/end street number, neighborhood, district, zip_code, town, main_address, type), values_*(id, attribute_id, category, attribute_name, value, outstanding, description), secondary_filters_* (empty), geo_epgs_25831_x/y, geo_epgs_4326_lat, geo_epgs_4326_lon, estimated_dates, start_date, end_date, timetable. One row per event. When `register_id, name, start_date, end_date` are all present the **dedicated map** is used (mode `dedicated`):
- title=`name`; venue=`addresses_road_name` + `addresses_start_street_number` (there is no venue column); city=`addresses_town` (default Barcelona); lat/lon=`geo_epgs_4326_lat/lon` (strings, may be empty); description=`values_description`.
- url: when `values_attribute_name` is "Venda d'entrades", "Informació" or "Inscripcions" and `values_value` is http(s), it is the event url.
- Keep rows whose start_date or end_date is >= today, and only rows whose `name` matches `concert|concierto|dj|festival|m[uú]sica|jazz|electr|live|club|sessi[oó]|session|gira|tour`.
- Multi-day entries (end_date > start_date) are festivals: start=start_date 00:00 with `time_unknown`, end=end_date. A span over 60 days is a permanent or recurring entry and is dropped. Single-day entries take the hour from `timetable` free text when it has one, else from `start_date` if it carries a time, else `time_unknown`.
- Dates are ISO (`YYYY-MM-DD`, sometimes with time and offset). ISO strings must be parsed **without** `dayfirst` (dateutil flips ambiguous ISO dates under `dayfirst=True`); other formats use `dayfirst=True`.
- This is the city's civic agenda, not club listings, so expect mostly promoter and festival "maybe" matches.

If the header is not the known one, `map_columns(headers)->dict` (heuristic mode) uses case-insensitive substring patterns, tried in this priority:
- title: nom|titol|title|name
- start: data_inici|inici|data|date|start
- end: data_fi|fi|final|end
- venue: equipament|lloc|venue|adre|address
- lat: latitud|lat
- lon: longitud|lon|lng
- url: enlla|url|link|entrades|web
- category: categori|tipus|classificaci|category
- description: descripci|description

Each column is claimed by one field only (first match wins in the order above). Unmapped columns are logged once at INFO. If the category column exists, keep rows matching (música|musica|concert|concierto|dj|club|festival|electr[oò]nica); if it doesn't exist, keep all. Dates use dateutil (dayfirst except for ISO). A row with no parseable date is skipped and counted. `source_ids` is `bcn:<hash(title|start|venue)>`. `inspect-csv` prints the resolved URL, headers, heuristic mapping, and 3 sample rows.

**Feeds.** feedparser per configured feed. Per entry: id = entry.id or link; skip if hash(id) in `state.feed_seen`. Combine title and summary and run artist, venue and promoter matching. On a match, emit an Event with start=None, tier heads-up, url=entry.link, title=entry.title. An unmatched entry is added to feed_seen without alerting. Cap feed_seen at 2000 (FIFO).

**ICS/webcal feeds.** `settings.feeds` entries may also be calendars. Type is auto-detected: the body starts with `BEGIN:VCALENDAR`, the URL ends with `.ics`, or it starts with `webcal://` (fetched as https). VEVENTs are parsed by hand after unfolding lines: UID, SUMMARY, DTSTART/DTEND (DATE, UTC `Z`, `TZID`, or floating = Madrid), LOCATION (venue = first comma segment; city = a matching region keyword), URL (else the first http link in DESCRIPTION), GEO, STATUS:CANCELLED. Artists = SUMMARY split on ` at ` / ` @ ` / ` - `, first segment. ICS entries are structured Events (start set, no `raw_tier_hint`), so they go through the normal geo filter and tier assignment: a must-see artist in a calendar triggers an immediate alert. They are not tracked in `feed_seen`.

**Inbox (optional; GMAIL_USER and GMAIL_APP_PASSWORD).** `imaplib.IMAP4_SSL("imap.gmail.com")`, select `gigwatch`, `SEARCH UNSEEN`, `FETCH (BODY.PEEK[])`, set `\Seen` only after successful processing. Parse with `email` (text/html preferred, then plain). `source_name_from_sender(domain)` maps ra.co→"Resident Advisor", dice.fm→"DICE", wegow.com→"Wegow", songkick.com→"Songkick", bandsintown.com→"Bandsintown", feverup.com→"Fever", ticketswap.com→"Ticketswap", xceed.me→"Xceed", otherwise the domain. Link choice: collect `<a href>` values, prefer hosts containing the sender's platform domain, then links whose text or URL contains ticket|tickets|entradas|buy|event; strip `utm_*`, `fbclid`, `mc_*` params. `source_ids` is `inbox:<sha1(message-id)>`; never persist body or subject text. Artist: run match_artist on subject+body text. Date: dateutil `fuzzy=True` on the first date-like regex hit, otherwise None. Tier: must-see if an artist matches (sent immediately), otherwise heads-up. Inbox is the legal path for RA, DICE, Wegow, Songkick and Bandsintown, whose ToS forbid automated access; we only read the user's own notification emails.

**Bandsintown (optional, BANDSINTOWN_APP_ID).** `GET https://rest.bandsintown.com/artists/{urlquote(name)}/events?app_id=...&date=upcoming` per followed artist. Map datetime, venue.{name,city,country,latitude,longitude}, offers[0].url, lineup, url, title. Terms intend the API for artists and partners; off unless the env var is set.

**Spotify import (CLI, local only).** PKCE (code_verifier 64 chars, S256), `http.server` redirect listener on 127.0.0.1:8888/callback, scope `user-follow-read`, `GET /v1/me/following?type=artist&limit=50&after=<cursor>` until `cursors.after` is null. Merge into artists.yaml by norm(name) (no duplicates; keep existing tier and aliases). Requires SPOTIFY_CLIENT_ID.

## 7. Pipeline (`run` in cli.py / `pipeline.py`)
1. Load config and state. `now` injectable for tests.
2. Fetch each enabled source inside try/except. Record ok/failed.
3. If no structured source (TM, BCN) succeeded and `now - state.last_source_warning_at > 24h`, send one Telegram warning and set the timestamp.
4. Filter: drop events with start in the past or beyond horizon; apply `in_region`. Heads-up events with start=None skip the geo filter.
5. Dedupe (section 8).
6. Assign tiers; drop untiered events.
7. Diff against state (section 9) producing the `Action` list.
8. Notify. 9. Render outputs (skip in dry-run). 10. Save state (skip in dry-run). 11. Summary line.

## 8. Dedupe (`dedupe.py`)
`key(ev)=sha1(f"{norm(primary_artist or title)}|{local_date}|{norm(venue or city or '')}")[:16]`. Primary artist is the first matched followed artist, else artists[0]. Events with no start use `sha1("nodate|"+sorted source_ids)`. `dedupe(events, priority)->list[Event]`:
1. Group by key.
2. Secondary pass: merge groups on the same local_date when EITHER (a) artist sets intersect AND both have coordinates less than 0.5 km apart, OR (b) `fuzz.ratio(norm(venue_a),norm(venue_b))>=90` AND (artist sets intersect OR `token_set_ratio(title_a,title_b)>=85`). Skip if either venue is empty.
   Venue names are compared after canonicalising through `venues.yaml` aliases (the display name is not changed). A third pass attaches a dated item with no venue (e.g. an email alert) to a venue-bearing group on the same local_date whose artist set intersects.
3. Merge: url from the highest-priority source; keep every source's url in `sources_urls`; union source_ids and artists; earliest fetched_at; status is the worst non-onsale status seen (canceled > postponed > rescheduled); keep non-None time, lat, lon from the most precise source (time_unknown is least precise). Merged uid is the key of the highest-priority member and stable across runs.

## 9. State and diff
`state.json`:
```json
{"version":1,"events":{"<uid>":{"first_seen":"iso","last_seen":"iso","notified_tier":"must-see","last_hash":"sha1","reminders_sent":["7d","1d","onsale"],"missed_runs":0,"possibly_removed":false,"digested":false,"snapshot":{"title":"","start":"","venue":"","city":"","url":"","status":"","artists":[],"tier":"","sources":[]}}},"last_digest_at":null,"last_source_warning_at":null,"inbox_seen":[],"feed_seen":[]}
```
`last_hash = sha1(start|status|bool(url)|venue)`.
- New uid with must-see tier: IMMEDIATE_NEW. A uid already notified at that tier is never re-alerted at that tier.
- Existing uid with changed hash: IMMEDIATE_CHANGE if tier is must-see and the change is start, canceled, postponed, rescheduled, or url appearing where there was none. Other changes just update the snapshot.
- Tier upgrade (maybe→must-see) counts as new for must-see.
- Maybe and heads-up events are marked for the digest (`digested:false`) and flip to `true` after sending.
- Reminders: must-see with future start: if `start - now <= 7d` and "7d" not in reminders_sent, send REMINDER_7D; same for 1d. If onsale_at has passed and "onsale" not recorded, send ON_SALE (only if event has url and status onsale). Mark reminders past their window as sent without sending (e.g. found with <1d left → both marked, since the NEW alert already carries the ⚠️).
- Lead warning: new must-see with `start - now < min_lead_days_warning` days gets ⚠️ prefix.
- Missing: events in state whose source was fetched successfully this run but weren't seen get `missed_runs+=1`; at 3, `possibly_removed=true` and mentioned in the next digest once. Reset `missed_runs=0` when seen again. Skip for sources that failed this run.
- Prune: events with start < now-7d removed. Events with no start pruned 60 days after last_seen.
- First run (state has no events and no last_digest_at): replace all IMMEDIATE_NEW with one summary message "First run: found N must-see shows" listing them (chunked to <4000 chars). Maybe events go to the first digest, sent immediately on first run.
- Overflow: if immediate Actions exceed `max_immediate` (15), send the first 15 by start date and move the rest into the digest with a note.
- Digest timing: compute the most recent `weekday/hour_local` instant D (Madrid tz) at or before now. Send if `last_digest_at is None or last_digest_at < D`. Digest contains all undigested maybe/heads-up events, held overflow, possibly_removed items. If empty, skip the send but still set `last_digest_at`. Digest uses disable_web_page_preview=True.

## 10. Telegram (`notify_telegram.py`)
`send(text, preview: bool)->bool`. POST `https://api.telegram.org/bot{token}/sendMessage` with chat_id, text, parse_mode=HTML, disable_web_page_preview=not preview. Retry up to 3 times with 2^n backoff on 5xx, on 429 sleep `parameters.retry_after`. Enforce >=1.1 s between messages. `html.escape` every dynamic field with `quote=True`, including URLs inside href. `format_event(ev, kind, distance)`:
```
🎯 MUST-SEE · <b>{artist}</b>
📅 Sat 15 Nov 2026 · 23:00
📍 {venue}, {city}{ · N km}
🎟 <a href="{url}">Tickets</a> · via Ticketmaster
(+ also: BCN Open Data, DICE email)
```
Prefixes: ⚠️ short notice, 🔄 CHANGED / ❌ CANCELED / ⏰ in 7 days / ⏰ tomorrow / 🎟 ON SALE NOW. Missing time omits ` · HH:MM`. Missing url omits the tickets line. Messages split at 4000 chars on line boundaries. `test-telegram` sends "gigwatch OK".

## 11. Outputs (`render.py`)
Pure functions with deterministic ordering and no timestamp noise (unchanged runs make no diff).
- `render_html(events)->str`: upcoming matched events with a start, must-see first, then maybe; grouped by date; ticket links; footer "Updated" derived from max fetched_at date only.
- `render_ics(events)->str`: VCALENDAR 2.0, PRODID `-//gigwatch//EN`; VEVENT per event with UID=`{uid}@gigwatch`, DTSTAMP=first_seen, DTSTART UTC, SUMMARY, LOCATION, URL, STATUS:CANCELLED for canceled. Fold at 75 octets, CRLF, escape `, ; \ newline`. Missing time → all-day (`VALUE=DATE`).
- `render_atom(events)->str`: Atom with xml.etree escaping, `<id>tag:gigwatch,{uid}</id>`, updated=first_seen.
- Write `docs/index.html`, `docs/gigwatch.ics`, `docs/feed.xml`. Pages on private repo needs GitHub Pro; fallback is making the repo public (contains only config and event data). `docs/COVERAGE.md` states what is not automated: Resident Advisor (ToS 4.4 bans automated extraction), DICE (no public API), Xceed, Fever, Ticketswap, Shotgun, Fourvenues, Instagram (Graph API can't read consumer accounts). For each, the manual step: follow artists/venues/promoters in that app and turn on email notifications routed by a Gmail filter to the `gigwatch` label, or push notifications. `SETUP.md` covers bot creation, secrets, Gmail filters/app password, TM key, Pages.

## 12. CLI
`run [--dry-run] [--config DIR] [--state FILE] [--out DIR] [--since-days N]`, `test-telegram`, `inspect-csv`, `import-spotify`, `add-artist NAME [--tier maybe]`, `list`. Defaults: config `config/`, state `state/state.json`, out `docs/`. `--dry-run` prints each message and writes no state or files. `quiet: true` suppresses reminders but not first-sight alerts. Final summary line: `sources ok=[..] failed=[..] fetched=N matched must=a maybe=b heads=c sent=m`.

## 13. GitHub Actions
`.github/workflows/gigwatch.yml`: schedule `17 */3 * * *` + workflow_dispatch; permissions contents: write; concurrency group gigwatch; checkout, setup-python 3.12 with pip cache, pip install -r requirements.txt, run with secrets as env, then commit `state docs` if changed as gigwatch-bot with "[skip ci]" and `git pull --rebase && git push`. Empty-string secrets are treated as unset (`os.environ.get(k) or None`). `pages.yml`: on push to `docs/**` + workflow_dispatch, configure-pages (continue-on-error), upload-pages-artifact (docs), deploy-pages; permissions pages: write, id-token: write.

## 14. Test plan
Unit (pytest, `now` injected): normalize; match (Âme vs Amelie Lens; Dixon vs Dixon Brothers; short names; Fontaines D.C. variants); geo; dedupe (TM+BCN+inbox merge, TM url kept, source_ids unioned); tiers; state diff (new, changed, canceled, postponed, url appears, no re-alert, tier upgrade); reminders; digest timing incl. DST; first-run summary; `--since-days`; Telegram escaping, 4000 split, overflow cap, 429 retry; CSV heuristics with Catalan and English header sets; inbox RA-style and DICE-style .eml fixtures; feeds; pruning and possibly_removed lifecycle; render ICS escaping/folding/stable UID, Atom/HTML well-formed.
Integration: `run --dry-run` with `responses` mocks for TM (2 pages), CKAN + CSV, feed, fake IMAP (monkeypatched). Asserts on printed messages and no files written. Second integration run (real write to tmp_path) confirms a repeat run sends nothing.
