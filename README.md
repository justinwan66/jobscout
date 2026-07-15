# JobScout

A personal job scouter: polls company job boards every minute (boards fetched in
parallel, a full sweep takes a few seconds), notifies you the moment a matching
job appears, and preps applications with your resume for one-click
review-then-submit.

## How it works

- **`scout.py`** polls the public JSON APIs of Greenhouse / Lever / Ashby
  company boards plus ServiceNow via SmartRecruiters (70 companies out of the box) every run, plus the
  broad remote-job APIs (Remotive) once an hour, plus **big-tech custom
  boards** (Google, Meta, Apple, Amazon, Netflix — none of which use a
  standard ATS) every 15 min via per-site scrapers: Amazon/Netflix JSON APIs,
  Google/Apple server-rendered HTML, Meta via a headless-browser GraphQL
  capture (`meta_board.py`, needs the `.venv` Playwright install). Big-tech
  scrapers are search-based (queries in `custom_boards.queries`), first page
  sorted newest. Matching is
  two-tier: 🔥 **early-career** roles (intern / co-op / new grad / campus +
  a data/analytics/quant/strategy stem) trigger instant notifications, and so
  does ⭐ **any match at a hand-review company** (the high-profile list where
  being early matters most); remaining relevant **full-time** roles
  (data / analytics / ML / strategy-ops, non-senior) are stored silently for
  browsing on the dashboard. Doctoral
  (PhD/postdoc) and senior+ titles are excluded outright, as are non-US
  locations outside your target metros.
- **`apply.py <job_url>`** opens the application page in a visible browser,
  fills your contact info from `profile.json`, attaches
  `~/git/resume/Justin_Wan_Resume.pdf`, then stops — **you** review and click
  Submit. It never submits on its own.
- **`auto_apply.py <job_id|url> [--dry-run]`** is zero-touch submission:
  headless browser fills the full form from `profile.json` + `answers.json`
  (work auth, EEO self-ID, GPA, availability), attaches resume (and transcript
  where asked), and submits — but ONLY if every required field was confidently
  filled and no CAPTCHA is present; otherwise the job lands in the dashboard's
  **Review** tab with a reason and a full-page screenshot in `logs/apps/`.
  Companies in `auto_apply.hand_review_companies` (config) are never
  auto-applied — they notify for manual tailoring instead. Per-company weekly
  cap: `max_per_company_per_week` (default 5).
- **`dashboard.py`** serves a local web UI at **http://localhost:8765** —
  browse/search all matches, open postings, one-click apply, and track your
  pipeline with Inbox / Review / Submitted / Hidden tabs (jobs grouped by day,
  company logos, gold border = high-profile, orange = early-career).
  Local-only (binds to 127.0.0.1).
- Two **launchd agents** run in the background while you're logged in:
  `com.justinwan66.jobscout` (poller, every 60 s) and
  `com.justinwan66.jobscout.dashboard` (web UI, kept alive).

## Commands

```bash
python3 ~/personal/jobscout/scout.py run          # poll once, notify new matches
python3 ~/personal/jobscout/scout.py run --seed   # record everything, notify nothing
python3 ~/personal/jobscout/scout.py list 30      # 30 most recent matches
python3 ~/personal/jobscout/scout.py test-notify  # test the notification path
~/personal/jobscout/.venv/bin/python ~/personal/jobscout/apply.py <job_url>    # prep an application for review
```

## Managing the background poller

```bash
launchctl unload ~/Library/LaunchAgents/com.justinwan66.jobscout.plist  # stop
launchctl load   ~/Library/LaunchAgents/com.justinwan66.jobscout.plist  # start
tail -f ~/personal/jobscout/logs/scout.log                                   # watch
```

## Configuration (`config.json`)

- **`boards`** — company slugs per ATS. Add any company: find its board URL
  (`boards.greenhouse.io/<slug>`, `jobs.lever.co/<slug>`,
  `jobs.ashbyhq.com/<slug>`) and add the slug. Invalid slugs just log a
  warning.
- **`custom_boards`** — big-tech scraper settings: `sites` toggles per
  company, `queries` are the search terms swept per site,
  `meta_queries` (fewer — each costs a browser page load), and
  `poll_every_minutes` (default 15). These scrape career *sites*, not APIs,
  so a site redesign can break one — it logs `WARN source failed` and the
  rest keep working.
- **`filters.include_title` / `exclude_title`** — case-insensitive substrings.
  Defaults target intern→entry-level data roles and exclude
  senior/staff/manager+.
- **`filters.location_filter`** — set to a regex (e.g. `"remote|ithaca|new
  york"`) to restrict locations; `null` = anywhere.
- **`notify.ntfy`** — phone push via [ntfy.sh](https://ntfy.sh) (enabled by
  default, works even when the Mac's notification is missed). One-time setup:
  install the ntfy app (iOS/Android) and subscribe to the topic in
  `config.json` (`notify.ntfy.topic`). The topic name is effectively the
  password — anyone who knows it can read the pushes, so keep it private.
- **`notify.email`** — set `"enabled": true` and export an app password as
  `JOBSCOUT_SMTP_PASSWORD`. For Cornell Gmail: Google Account → Security →
  2-Step Verification → App passwords. To make it work under launchd, add
  inside the plist's `<dict>`:

  ```xml
  <key>EnvironmentVariables</key>
  <dict><key>JOBSCOUT_SMTP_PASSWORD</key><string>xxxx xxxx xxxx xxxx</string></dict>
  ```

## Apply helper setup (one-time)

```bash
python3 -m pip install playwright
python3 -m playwright install chromium
```

## Notes & limits

- First run auto-seeds (stores current postings silently) so you aren't
  spammed with 100+ notifications; only jobs appearing *after* that notify.
- WeWorkRemotely is disabled — it sits behind a Cloudflare bot challenge.
- Fully unattended auto-apply was deliberately left out: ATS forms vary,
  many include custom/visa questions, and unreviewed mass applications hurt
  more than help. `apply.py` gets you to ~10 seconds per application instead.
- If macOS notifications don't appear: System Settings → Notifications →
  Script Editor (or osascript) → Allow.
