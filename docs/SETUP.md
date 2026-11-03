# Setup

Everything below is free. You need a GitHub account, a Telegram account and (optionally) a Gmail account.

## 1. Create the repo

Create the repository `themetaisok/gigwatch` and push this project to it. A **public** repo is fine and recommended: it holds only config and event data, never secrets (those live in GitHub Actions secrets), and public repos get GitHub Pages for free. The workflow commits `state/` and `docs/` back to the repo, so it needs write access (the workflow already sets `permissions: contents: write`).

## 2. Telegram bot

1. In Telegram, open a chat with **@BotFather** and send `/newbot`. Follow the prompts. BotFather gives you a token like `123456:ABC...`. This is `TELEGRAM_BOT_TOKEN`.
2. Open a chat with your new bot and press **Start** (a bot cannot message you until you do).
3. In a browser open `https://api.telegram.org/bot<TOKEN>/getUpdates`, replacing `<TOKEN>` with your token. Find `"chat":{"id":123456789,...}`. That number is `TELEGRAM_CHAT_ID`.
4. Test locally: `TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python -m gigwatch test-telegram`. You should receive "gigwatch OK".

## 3. Ticketmaster key

Register at https://developer.ticketmaster.com, create an app, and copy the **Consumer Key**. This is `TICKETMASTER_API_KEY`. The free tier allows 5000 calls a day; gigwatch uses well under that.

## 4. GitHub secrets

In the repo go to Settings, Secrets and variables, Actions, and add:

| Secret | Required | Value |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | from BotFather |
| `TELEGRAM_CHAT_ID` | yes | from getUpdates |
| `TICKETMASTER_API_KEY` | recommended | Ticketmaster consumer key |
| `GMAIL_USER` | optional | your Gmail address |
| `GMAIL_APP_PASSWORD` | optional | Gmail app password (see below) |
| `BANDSINTOWN_APP_ID` | optional | your Bandsintown app id |

Empty secrets are treated as unset, so you can leave optional ones blank.

## 5. Gmail (optional, covers RA, DICE, Wegow, Songkick and more)

1. In Gmail create a label named `gigwatch`.
2. For each sender below, create a filter (Settings, Filters and blocked addresses, Create a new filter) with **From** set to the domain, and choose **Apply the label: gigwatch** (also tick "Skip the inbox" if you like):
   - `ra.co` (Resident Advisor)
   - `dice.fm` (DICE)
   - `wegow.com`
   - `songkick.com`
   - `bandsintown.com`
   - `feverup.com`
   - `ticketswap.com`
   - `xceed.me`
3. In each app, follow your artists, venues and promoters and turn on email notifications.
4. Turn on 2-step verification for your Google account (required for app passwords), then go to https://myaccount.google.com/apppasswords, create an app password and use it as `GMAIL_APP_PASSWORD`. Never use your normal password.
5. IMAP must be enabled (Gmail settings, Forwarding and POP/IMAP).

gigwatch only reads unread mail in the `gigwatch` label, marks it read after processing, and never stores email subjects or bodies.

## 6. Bandsintown (optional)

If you have an app id, set `BANDSINTOWN_APP_ID`. Their terms intend the API for artists and partners, so it is off unless you set it.

## 7. Spotify import (optional, local only)

1. Create an app at https://developer.spotify.com/dashboard and add the redirect URI `http://127.0.0.1:8888/callback`.
2. Run `SPOTIFY_CLIENT_ID=<id> python -m gigwatch import-spotify`. A browser opens to authorise. Followed artists are merged into `config/artists.yaml` as `maybe` (existing entries are kept as they are). Promote the ones you love to `must-see`.

## 8. GitHub Pages

gigwatch writes the web page, `gigwatch.ics` and `feed.xml` into `docs/` and commits them. There is no Pages workflow: in the repo go to Settings, Pages, set **Source: Deploy from a branch**, branch `main`, folder `/docs`. GitHub republishes whenever the bot pushes. For a public repo this is free, and the site is at https://themetaisok.github.io/gigwatch/ (`docs/.nojekyll` keeps GitHub from processing the files). If you make the repo private instead, Pages needs a paid GitHub plan.

## 9. Add an artist

Easiest, from your phone: open `config/artists.yaml` on github.com, tap the pencil icon, add a line, commit. The next run (within 3 hours) picks it up. Locally, run `python -m gigwatch add-artist "Artist Name" --tier must-see`, or edit `config/artists.yaml` by hand:

```yaml
- name: Dixon
  tier: must-see          # must-see = instant alert, maybe = weekly digest
  aliases: []             # other spellings
  exclude: [dixon brothers]   # phrases that void a match
```

Commit and push. The next run picks it up.

## 10. Subscribe to the calendar

Once Pages is live, subscribe to `https://themetaisok.github.io/gigwatch/gigwatch.ics` in Apple Calendar, Google Calendar (From URL) or Outlook. The Atom feed is at `.../feed.xml`.

## 11. First run

Install with `pip install -r requirements.txt` (runtime) or `requirements-dev.txt` (adds pytest). Try it locally first: `python -m gigwatch run --dry-run`. It prints each message and writes nothing. Then run the **gigwatch** workflow manually (Actions tab, Run workflow). The first real run sends one summary message plus the first digest.

If the workflow log ends with a failed send, the run exits non-zero but still commits state, so nothing is lost or repeated.

## Running without Ticketmaster

The Ticketmaster key is optional. Without it only the Barcelona cultural agenda (festivals, concerts, promoter matches) is active, and the run log says so. Add feeds, `.ics` calendars or the Gmail route (sections 5 and "Other feeds") for club coverage.

## Other feeds

Add RSS/Atom feeds (for example Google Alerts delivered as an RSS feed) or `.ics` / `webcal://` calendars under `feeds:` in `config/settings.yaml`. Calendars produce real events that go through the normal region filter and tiers.
