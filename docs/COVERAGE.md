# Coverage

What gigwatch looks at, and what it will miss.

| Source | Layer | How | Automated? | What it will miss |
|---|---|---|---|---|
| Ticketmaster Discovery API | artists + scene | Geo search around Barcelona (150 km) plus one keyword query per followed artist | Yes | Anything not sold through Ticketmaster: most club nights, small venues, many festivals |
| Barcelona Open Data (Agenda Cultural) | scene | Daily CSV from the city open data portal, filtered to music-like names | Yes, but currently blocked: the portal returns HTTP 403 to GitHub Actions IPs (verified 4 Nov 2026). Works when run from a home network | Club nights and private promoters. It is a civic agenda (festivals, concerts, classical cycles), updated daily, so it mostly yields festival and promoter "maybe" matches |
| Bandsintown API | artists | Per-artist upcoming events | Only with `BANDSINTOWN_APP_ID` | Artists who do not use Bandsintown. Terms intend the API for artists and partners, so it is off by default |
| Gmail notification emails | artists + scene | You follow artists, venues and promoters in each app and turn on email notifications. A Gmail filter labels them `gigwatch`. gigwatch reads only that label | Yes, once you set up the filters | Anything you did not follow or enable emails for. It only sees the email, so a missing date means a heads-up in the digest, not an instant alert |
| Resident Advisor | artists + scene | Via Gmail emails only. Terms (4.4) ban automated extraction | No, email route only | Everything unless you follow it in the RA app with email on |
| DICE | artists + scene | Via Gmail emails only. No public API | No, email route only | As above |
| Wegow, Songkick | artists + scene | Via Gmail emails only | No, email route only | As above |
| Xceed, Fever, Ticketswap, Shotgun, Fourvenues | scene | Not automated. Follow artists, venues and promoters in the app and turn on email notifications (routed to the `gigwatch` label by a Gmail filter) or push notifications | No | Everything not routed by email |
| Instagram | scene | Not automated. The Graph API cannot read consumer accounts. Turn on post notifications for key venues and promoters in the app | No | All announcements made only on Instagram |
| RSS / Atom feeds (for example Google Alerts) | artists + scene | Listed under `feeds` in `settings.yaml`. Entries that mention a followed artist, venue or promoter become heads-up items in the digest | Yes | News without a date. Google Alerts is noisy and delayed |
| ICS / webcal calendars | scene | Listed under `feeds`. Each event goes through the same region filter and tiers as everything else, so a must-see artist triggers an instant alert | Yes | Whatever the calendar owner leaves out |

## Notes

- Tiers: **must-see** artists alert immediately, everything else (maybe artists, venues, promoters, genres) goes into a weekly digest.
- The region filter uses coordinates when a source provides them, and place names otherwise.
- Events removed from a source are flagged "possibly removed" in the next digest after three missed runs, rather than silently disappearing.
