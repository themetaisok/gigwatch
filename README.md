# gigwatch

Personal gig alerts for Barcelona. Every 3 hours it checks Ticketmaster, the Barcelona Open Data cultural agenda, optional feeds and calendars, and your own Gmail notification emails. It sends you a Telegram message for artists you must not miss, a weekly digest for the maybes, and publishes a web page, an `.ics` calendar and an Atom feed.

## Quickstart

1. Create a GitHub repo and push this project (public is fine: it holds only config and event data, never secrets; public also gives you free GitHub Pages). See `docs/SETUP.md`.
2. Create a Telegram bot with BotFather and find your chat id.
3. Get a free Ticketmaster API key.
4. Add the GitHub secrets `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TICKETMASTER_API_KEY` (optional: `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `BANDSINTOWN_APP_ID`).
5. Edit `config/artists.yaml`, then run the **gigwatch** workflow once from the Actions tab.

## Commands

```
python -m gigwatch run [--dry-run] [--config DIR] [--state FILE] [--out DIR] [--since-days N]
python -m gigwatch test-telegram
python -m gigwatch inspect-csv [--file PATH]
python -m gigwatch import-spotify [--config DIR] [--tier maybe|must-see]
python -m gigwatch add-artist "Name" [--tier maybe] [--config DIR]
python -m gigwatch list [--config DIR]
```

Local setup: `python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt && .venv/bin/pytest -q`.

### Names that are also common words

An artist can set `title_match` in `config/artists.yaml` to control what a hit found **only in an event title** is worth:

- `must-see` (default): same as a lineup match.
- `maybe`: the hit goes to the weekly digest instead of an instant alert. Use it for Jungle, Arca, Black Coffee and similar ("Jungle Book", "Arca de Noé").
- `off`: ignore title hits entirely; only an exact or fuzzy lineup match counts.

Lineup (attraction) matches and your own notification emails are never downgraded.

See `docs/SETUP.md` for setup, `docs/COVERAGE.md` for what is and is not covered, and `DESIGN.md` for the design.
