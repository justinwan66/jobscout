#!/usr/bin/env python3
"""JobScout — personal job-opening watcher.

Polls company job boards (Greenhouse / Lever / Ashby public APIs) plus broad
remote-job sources (Remotive, RemoteOK, WeWorkRemotely), filters titles against
your target roles, and notifies you (macOS notification + ntfy.sh phone push +
optional email) the first time each matching job is seen.

Usage:
  python3 scout.py run            # poll once; notify about new matches
  python3 scout.py run --seed     # poll once; record everything, notify nothing
  python3 scout.py list [N]       # show the N most recent matches (default 20)
  python3 scout.py test-notify    # fire a test macOS notification / email
"""

import gzip
import html
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
import smtplib
import sqlite3
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.message import EmailMessage
from hashlib import sha1
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG = json.loads((BASE / "config.json").read_text())
DB_PATH = BASE / "jobs.db"
LOG_PATH = BASE / "logs" / "scout.log"

def company_key(s):
    """Normalize a company name so an ATS 'Scale AI' matches config 'scaleai'
    — same rule the auto-apply hand-review gate uses, so routing stays in sync."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


# high-profile companies land straight in the Review bin (auto-fill on demand,
# you review + submit) instead of the auto-apply inbox
HAND_REVIEW = {company_key(c) for c in
               CONFIG.get("auto_apply", {}).get("hand_review_companies", [])}

SSL_CTX = ssl.create_default_context()
UA = {"User-Agent": "jobscout-personal/1.0 (individual job seeker)"}
# big-tech career sites serve different (or no) content to non-browser agents
BROWSER_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}


def log(msg):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line)
    LOG_PATH.parent.mkdir(exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def get(url, timeout=15, headers=None):
    # request gzip: the biggest boards (Anduril ~2.3MB, SpaceX) stream multi-MB
    # bodies uncompressed and trip the read timeout under the parallel sweep;
    # gzipped they are ~18x smaller. urllib does NOT auto-decompress, so do it.
    hdrs = dict(headers or UA)
    hdrs.setdefault("Accept-Encoding", "gzip")
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        data = r.read()
        if data and r.headers.get("Content-Encoding", "").lower() == "gzip":
            data = gzip.decompress(data)
        return data


def get_json(url, timeout=15, headers=None):
    return json.loads(get(url, timeout, headers))


def strip_html(s):
    return re.sub(r"<[^>]+>", " ", html.unescape(s or ""))


def _amazon_date(s):
    """Amazon posts a human date like 'July 14, 2026' -> ISO 8601, or None."""
    try:
        return datetime.strptime(s, "%B %d, %Y").replace(
            tzinfo=timezone.utc).isoformat()
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------- sources

def fetch_greenhouse(slug):
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    for j in data.get("jobs", []):
        yield {
            "source": "greenhouse",
            "company": slug,
            "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url", ""),
            "posted_at": j.get("first_published") or j.get("updated_at"),
            "ext_id": j.get("id"),
        }


def fetch_lever(slug):
    data = get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    for j in data:
        yield {
            "source": "lever",
            "company": slug,
            "title": j.get("text", ""),
            "location": (j.get("categories") or {}).get("location", "") or "",
            "url": j.get("hostedUrl", ""),
            "posted_at": (datetime.fromtimestamp(j["createdAt"] / 1000,
                          timezone.utc).isoformat() if j.get("createdAt") else None),
            "description": j.get("descriptionPlain", "") + " " + " ".join(
                strip_html(sec.get("content", "")) for sec in j.get("lists", [])),
        }


def fetch_ashby(slug):
    data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        yield {
            "source": "ashby",
            "company": slug,
            "title": j.get("title", ""),
            "location": j.get("location", "") or "",
            "url": j.get("jobUrl", "") or j.get("applyUrl", ""),
            "posted_at": j.get("publishedAt"),
            "description": j.get("descriptionPlain", ""),
        }


def fetch_smartrecruiters(slug):
    offset = 0
    while offset < 3000:
        data = get_json("https://api.smartrecruiters.com/v1/companies/"
                        f"{slug}/postings?limit=100&offset={offset}")
        postings = data.get("content", [])
        for j in postings:
            loc = j.get("location") or {}
            parts = [loc.get("city", ""), loc.get("region", ""),
                     (loc.get("country") or "").upper()]
            yield {
                "source": "smartrecruiters",
                "company": slug,
                "title": j.get("name", ""),
                "location": ", ".join(p for p in parts if p),
                "url": f"https://jobs.smartrecruiters.com/{slug}/{j.get('id')}",
                "posted_at": j.get("releasedDate"),
                "ext_id": j.get("id"),
            }
        offset += 100
        if offset >= data.get("totalFound", 0) or not postings:
            break


def fetch_remotive():
    data = get_json("https://remotive.com/api/remote-jobs?category=data")
    for j in data.get("jobs", []):
        yield {
            "source": "remotive",
            "company": j.get("company_name", ""),
            "title": j.get("title", ""),
            "location": j.get("candidate_required_location", "") or "Remote",
            "url": j.get("url", ""),
            "posted_at": j.get("publication_date"),
            "description": j.get("description", ""),
        }


def fetch_remoteok():
    data = get_json("https://remoteok.com/api")
    for j in data:
        if not isinstance(j, dict) or not j.get("position"):
            continue  # first element is a legal notice
        yield {
            "source": "remoteok",
            "company": j.get("company", ""),
            "title": j.get("position", ""),
            "location": j.get("location", "") or "Remote",
            "url": j.get("url", ""),
        }


def fetch_weworkremotely():
    raw = get("https://weworkremotely.com/categories/remote-data-jobs.rss")
    root = ET.fromstring(raw)
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        url = (item.findtext("link") or "").strip()
        company, _, job_title = title.partition(":")
        yield {
            "source": "weworkremotely",
            "company": company.strip(),
            "title": (job_title or title).strip(),
            "location": "Remote",
            "url": url,
        }


# -------------------------------------------------- big-tech custom boards
# Google / Meta / Apple / Amazon / Netflix run custom career sites with no
# Greenhouse-style full-listing API, so each gets a search-based fetcher:
# first page sorted newest, queries from config custom_boards.queries. New
# postings surface at the top, so the poll cadence catches them early.

def fetch_amazon(query):
    q = urllib.parse.quote_plus(query)
    data = get_json("https://www.amazon.jobs/en/search.json?base_query="
                    f"{q}&result_limit=100&offset=0&sort=recent",
                    headers=BROWSER_UA)
    for j in data.get("jobs", []):
        yield {
            "source": "amazon",
            "company": "amazon",
            "title": j.get("title", ""),
            "location": j.get("normalized_location") or j.get("location", ""),
            "url": "https://www.amazon.jobs" + (j.get("job_path") or ""),
            "posted_at": _amazon_date(j.get("posted_date")),
            # no description here: the search teaser proved unreliable (missed
            # full qualifications). get_description falls through to
            # full_description(), which parses the complete HTML posting.
        }


GOOGLE_DATA_RE = re.compile(
    r"AF_initDataCallback\(\{key: 'ds:1'.*?data:(.*?), sideChannel", re.S)


def fetch_google(query):
    q = urllib.parse.quote_plus(query)
    raw = get("https://www.google.com/about/careers/applications/jobs/results?"
              f"q={q}&location=United+States&sort_by=date",
              headers=BROWSER_UA).decode()
    m = GOOGLE_DATA_RE.search(raw)
    if not m:
        raise ValueError("embedded job data not found (page layout changed?)")
    for j in json.loads(m.group(1))[0]:
        yield {
            "source": "google",
            "company": "google",
            "title": j[1] or "",
            "location": "; ".join(loc[0] for loc in (j[9] or []) if loc),
            "url": ("https://www.google.com/about/careers/applications/"
                    f"jobs/results/{j[0]}"),
            "description": " ".join((f or [None, ""])[1] or ""
                                    for f in (j[3], j[4])),
        }


def fetch_netflix(query):
    q = urllib.parse.quote_plus(query)
    for start in (0, 10, 20):  # API caps at 10 positions per request
        data = get_json("https://explore.jobs.netflix.net/api/apply/v2/jobs?"
                        f"domain=netflix.com&query={q}&sort_by=timestamp"
                        f"&start={start}", headers=BROWSER_UA)
        positions = data.get("positions", [])
        for p in positions:
            yield {
                "source": "netflix",
                "company": "netflix",
                "title": p.get("name", ""),
                "location": ("; ".join(p.get("locations") or [])
                             or p.get("location", "")),
                "url": p.get("canonicalPositionUrl", ""),
            }
        if len(positions) < 10:
            break


# jobs.apple.com is server-side rendered; its JSON API silently returns zero
# results without a browser session, so parse the HTML job list instead
APPLE_TITLE_RE = re.compile(
    r'<a class="link-inline[^"]*"[^>]*href="(/en-us/details/[^"]+)"[^>]*>'
    r'([^<]+)</a>')
APPLE_LOC_RE = re.compile(r'>Location</span><span[^>]*>([^<]*)<')


def fetch_apple(query):
    q = urllib.parse.quote_plus(query)
    raw = get(f"https://jobs.apple.com/en-us/search?search={q}&sort=newest",
              headers=BROWSER_UA).decode()
    items = raw.split('class="rc-accordion-item"')[1:]
    if not items:
        raise ValueError("no job list items found (page layout changed?)")
    for item in items:
        m = APPLE_TITLE_RE.search(item)
        if not m:
            continue
        path, title = m.groups()
        lm = APPLE_LOC_RE.search(item)
        yield {
            "source": "apple",
            "company": "apple",
            "title": html.unescape(title).strip(),
            "location": html.unescape(lm.group(1)).strip() if lm else "",
            "url": "https://jobs.apple.com" + path.split("?")[0],
            "description": item,  # SSR item embeds the full job description
        }


def fetch_meta(queries):
    """metacareers.com only exposes jobs via in-browser GraphQL, so shell out
    to meta_board.py under the Playwright venv (one browser for all queries)."""
    venv_py = BASE / ".venv" / "bin" / "python"
    if not venv_py.exists():
        raise RuntimeError("meta: needs .venv with playwright installed")
    out = subprocess.run(
        [str(venv_py), str(BASE / "meta_board.py"), *queries],
        capture_output=True, text=True, timeout=180)
    if out.returncode != 0:
        raise RuntimeError(f"meta_board.py: {out.stderr.strip()[-300:]}")
    yield from json.loads(out.stdout)


# ---------------------------------------------------------------- matching

def compile_filters():
    f = CONFIG["filters"]

    def c(key, raw=False):
        return [re.compile(p if raw else re.escape(p), re.I) for p in f[key]]

    loc_inc = f.get("location_include")
    loc_exc = f.get("location_exclude")
    loc_res = (re.compile(loc_inc, re.I) if loc_inc else None,
               re.compile(loc_exc, re.I) if loc_exc else None)
    return c("include_title"), c("exclude_title"), \
        (c("early_career_title", raw=True), c("early_career_stems")), loc_res


def location_ok(location, loc_res):
    loc_inc, loc_exc = loc_res
    if not location:
        return True  # unspecified location: let it through rather than miss it
    if loc_exc and loc_exc.search(location):
        return False
    if loc_inc and not loc_inc.search(location):
        return False
    return True


def matches(job, inc, exc, early, loc_res):
    """Tier a job: 'hot' = early-career role you can act on (notifies),
    'match' = relevant full-time role (stored silently), None = irrelevant."""
    title = job["title"]
    if any(p.search(title) for p in exc):
        return None
    if not location_ok(job["location"], loc_res):
        return None
    markers, stems = early
    if any(p.search(title) for p in markers) and any(p.search(title) for p in stems):
        return "hot"
    if any(p.search(title) for p in inc):
        return "match"
    return None


def get_description(job):
    """Return plain-text description; fetched lazily for sources whose
    listing feed doesn't include it (only called for new matched jobs)."""
    if job.get("description"):
        return strip_html(job["description"])
    if job["source"] == "greenhouse" and job.get("ext_id"):
        d = get_json("https://boards-api.greenhouse.io/v1/boards/"
                     f"{job['company']}/jobs/{job['ext_id']}")
        return strip_html(html.unescape(d.get("content", "")))
    if job["source"] == "smartrecruiters" and job.get("ext_id"):
        d = get_json("https://api.smartrecruiters.com/v1/companies/"
                     f"{job['company']}/postings/{job['ext_id']}")
        secs = (d.get("jobAd") or {}).get("sections") or {}
        return strip_html(" ".join(s.get("text", "") for s in secs.values()
                                   if isinstance(s, dict)))
    if job["source"] == "netflix":
        pid = job["url"].rstrip("/").rsplit("/", 1)[-1]
        d = get_json("https://explore.jobs.netflix.net/api/apply/v2/jobs/"
                     f"{pid}?domain=netflix.com", headers=BROWSER_UA)
        return strip_html(d.get("job_description", ""))
    return ""


YEARS_PATTERNS = [re.compile(p, re.I) for p in (
    r"(\d{1,2})\s*\+\s*(?:years?|yrs?)",
    r"minimum (?:of )?(\d{1,2})\s*(?:years?|yrs?)",
    r"at least (\d{1,2})\s*(?:years?|yrs?)",
    r"(\d{1,2})\s*or more (?:years?|yrs?)",
    # "3-5 years", "3 to 5 years" — gate on the range's lower bound
    r"(\d{1,2})\s*(?:-|–|—|to)\s*\d{1,2}\s*(?:years?|yrs?)",
    # bare "5 years of relevant/professional/... experience"
    r"(\d{1,2})\s*\+?\s*(?:years?|yrs?)(?:['’]s?)?\s+(?:of\s+)?"
    r"(?:[a-z-]+\s+){0,3}experience")]

# a master's/graduate degree named with no bachelor's path anywhere in the
# posting is a hard gate for a current undergrad, even without "required"
# bare "MS" only counts followed by in/degree — else it hits "MS Office" etc.
MASTERS_RE = re.compile(r"(?:master'?s?|graduate)\s+degree|\bm\.s\.|\bmsc\b"
                        r"|\bm\.?s\.?\s+(?:in|degree)", re.I)
BACHELORS_RE = re.compile(
    r"bachelor|\bb\.?s\.?\b|\bba\b|undergraduate\s+(?:degree|student)|"
    r"currently\s+(?:enrolled|pursuing)", re.I)


def screen_description(text):
    """('exclude', why) | ('promote', why) | ('ok', '') from a description."""
    f = CONFIG.get("description_filters", {})
    t = text.lower()
    for pat in f.get("exclude", []):
        if re.search(pat, t, re.I):
            return "exclude", f"description matches '{pat[:40]}'"
    cap = f.get("max_required_years", 2)
    for pat in YEARS_PATTERNS:
        for m in pat.finditer(t):
            if int(m.group(1)) > cap:
                return "exclude", f"requires {m.group(1)}+ years experience"
    if MASTERS_RE.search(t) and not BACHELORS_RE.search(t):
        return "exclude", "requires a graduate degree (no bachelor's path)"
    for pat in f.get("promote_hot", []):
        if re.search(pat, t, re.I):
            return "promote", f"description matches '{pat[:40]}'"
    return "ok", ""


LLM_PROMPT = """You screen job postings for Justin: a Cornell undergrad \
(B.S. Biometry & Statistics, expected May 2028), currently a data-analytics \
intern with ~1 year of internship experience, seeking internships, co-ops, \
and entry-level/new-grad data, analytics, ML, or strategy-ops roles.

Regex filters already rejected postings with explicit blockers (>2 years \
required, grad degree required, senior titles). Your job is the subtler \
tail: postings whose scope, level, or phrasing implies they would never \
seriously consider a current undergrad (e.g. "own the roadmap", \
"founding/first hire", "shape company strategy", implied 5+ years, deep \
domain mastery expected on day one).

Could Justin CREDIBLY apply — meaning a recruiter would plausibly consider \
him rather than screen him out instantly?

Reply with EXACTLY one line:
PASS
or
FAIL: <short reason, under 12 words>

Posting follows:
"""


def _llm_via_api(prompt, model, api_key, max_tokens=50):
    """Direct Anthropic API call — portable to any machine / CI with a key."""
    body = json.dumps({"model": model, "max_tokens": max_tokens,
                       "messages": [{"role": "user", "content": prompt}]})
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body.encode(),
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=60, context=SSL_CTX) as r:
        data = json.loads(r.read())
    return "".join(b.get("text", "") for b in data.get("content", []))


def _llm_via_cli(prompt, model, cmd):
    """Claude Code CLI — uses the local subscription login; no API key needed.
    --permission-mode bypassPermissions stops the CLI prompting for file/data
    access on every spawn (safe here: a text-only completion, no tools)."""
    r = subprocess.run(
        [cmd, "-p", "--permission-mode", "bypassPermissions", "--model", model],
        input=prompt, capture_output=True, text=True, timeout=90)
    return r.stdout or ""


def llm_complete(prompt, max_tokens=50):
    """Raw LLM completion via API key (anywhere) or local claude CLI.
    Raises on no-backend / transport error; callers decide fail-open."""
    cfg = CONFIG.get("llm_screen", {})
    model = cfg.get("model", "claude-haiku-4-5-20251001")
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    cmd = cfg.get("command", "claude")
    if api_key:
        return _llm_via_api(prompt, model, api_key, max_tokens)
    if Path(cmd).exists() or subprocess.run(
            ["which", cmd], capture_output=True).returncode == 0:
        return _llm_via_cli(prompt, model, cmd)
    raise RuntimeError("no ANTHROPIC_API_KEY and no claude CLI")


def llm_screen(title, desc):
    """('ok'|'exclude'|'skip', why) — final judge on posts the regexes pass.
    Uses ANTHROPIC_API_KEY when set (works anywhere, incl. CI), else the
    local claude CLI. Fail-open: any error/timeout keeps the job ('skip')."""
    cfg = CONFIG.get("llm_screen", {})
    if not cfg.get("enabled") or not desc:
        return "skip", ""
    prompt = (LLM_PROMPT + f"TITLE: {title}\n\n"
              + desc[:cfg.get("max_desc_chars", 6000)])
    try:
        raw = llm_complete(prompt)
        out = raw.strip().splitlines()
        verdict = out[-1].strip() if out else ""
        if verdict.upper().startswith("FAIL"):
            return "exclude", (verdict.partition(":")[2].strip()
                               or "LLM: not credible for undergrad")
        if verdict.upper().startswith("PASS"):
            return "ok", ""
        return "skip", f"unparseable verdict: {verdict[:40]}"
    except Exception as e:
        return "skip", str(e)[:60]


# ---------------------------------------------------------------- storage

def db_connect():
    # WAL + a generous busy_timeout so the poller no longer crashes with
    # "database is locked" when a spawned writer (reconcile/precheck) or the
    # dashboard holds the write lock longer than the old 5s default.
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("""CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY, source TEXT, company TEXT, title TEXT,
        location TEXT, url TEXT, tier TEXT, first_seen TEXT, status TEXT DEFAULT 'new')""")
    con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    for col in ("reason TEXT", "applied_at TEXT",
                "readiness TEXT", "readiness_at TEXT", "posted_at TEXT"):
        try:
            con.execute(f"ALTER TABLE jobs ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass  # column already exists
    return con


def job_id(job):
    return sha1(job["url"].encode()).hexdigest()


# ---------------------------------------------------------------- notify

def notify_macos(title, message, url=None):
    if not CONFIG["notify"]["macos_notification"]:
        return
    script = (f'display notification "{message.replace(chr(34), chr(39))}" '
              f'with title "{title.replace(chr(34), chr(39))}" sound name "Glass"')
    subprocess.run(["osascript", "-e", script], capture_output=True)


def notify_email(new_jobs):
    cfg = CONFIG["notify"]["email"]
    if not cfg.get("enabled"):
        return
    import os
    # send_from gates which poller emails (mirrors ntfy) so the local and cloud
    # pollers don't both email. 'cloud' = only the 24/7 GitHub Actions poller
    # emails, which is what gives coverage while the Mac is asleep/off.
    is_cloud = os.environ.get("JOBSCOUT_CLOUD") == "1"
    mode = cfg.get("send_from", "cloud")
    if (mode == "cloud" and not is_cloud) or (mode == "local" and is_cloud):
        return
    password = os.environ.get(cfg["smtp_password_env"], "")
    if not password:
        log(f"email skipped: {cfg['smtp_password_env']} not set")
        return
    rows = "".join(
        f'<li><b>{html.escape(j["company"])}</b> — '
        f'<a href="{html.escape(j["url"])}">{html.escape(j["title"])}</a> '
        f'({html.escape(j["location"] or "n/a")})'
        f'{" 🔥" if j["tier"] == "hot" else ""}</li>'
        for j in new_jobs)
    msg = EmailMessage()
    msg["Subject"] = f"JobScout: {len(new_jobs)} new matching job(s)"
    msg["From"] = cfg["from"]
    msg["To"] = cfg["to"]
    msg.set_content("\n".join(f'{j["company"]}: {j["title"]} — {j["url"]}' for j in new_jobs))
    msg.add_alternative(
        f"<p>New matches (🔥 = intern/entry-level marker in title):</p><ul>{rows}</ul>"
        f"<p>To prep an application: <code>~/personal/jobscout/.venv/bin/python ~/personal/jobscout/apply.py &lt;url&gt;</code></p>",
        subtype="html")
    with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"]) as s:
        s.starttls(context=SSL_CTX)
        s.login(cfg["smtp_user"], password)
        s.send_message(msg)
    log(f"email sent to {cfg['to']} ({len(new_jobs)} jobs)")


def _ntfy_post(topic, title, body, url=None):
    headers = {**UA, "Title": title.encode("ascii", "ignore").decode()}
    if url:
        headers["Click"] = url
    req = urllib.request.Request(f"https://ntfy.sh/{topic}",
                                 data=body.encode(), headers=headers)
    urllib.request.urlopen(req, timeout=10, context=SSL_CTX).read()


def notify_ntfy(new_jobs):
    """Phone push via ntfy.sh — works anywhere, even with the laptop asleep
    elsewhere; subscribe to the configured topic in the ntfy app.
    send_from: 'cloud' = only the GitHub Actions poller pushes (default once
    cloud polling exists — avoids double pushes), 'local'/'both' as named."""
    cfg = CONFIG["notify"].get("ntfy", {})
    if not cfg.get("enabled") or not cfg.get("topic"):
        return
    is_cloud = os.environ.get("JOBSCOUT_CLOUD") == "1"
    mode = cfg.get("send_from", "both")
    if (mode == "cloud" and not is_cloud) or (mode == "local" and is_cloud):
        return
    cap = CONFIG["notify"]["max_individual_notifications"]
    try:
        if len(new_jobs) <= cap:
            for j in new_jobs:
                _ntfy_post(cfg["topic"], f"New role: {j['company']}",
                           f"{j['title']} — {j['location'] or 'n/a'}", j["url"])
        else:
            _ntfy_post(cfg["topic"], "JobScout",
                       f"{len(new_jobs)} new roles — see localhost:8765")
    except Exception as e:
        log(f"WARN ntfy push failed: {e}")


# ---------------------------------------------------------------- main

CUSTOM_FETCHERS = {"amazon": fetch_amazon, "google": fetch_google,
                   "netflix": fetch_netflix, "apple": fetch_apple}

# search-based big-tech boards repost the same role under a new URL (Amazon
# reposts sequentially) and surface one role under several query terms. job_id
# is a URL hash, so it treats each as new — dedup these by (source, company,
# title) instead. Full-listing ATS boards don't need this.
DEDUP_BY_TITLE = {"amazon", "google", "netflix", "apple"}


def collect_jobs(include_broad, include_custom=False):
    jobs, errors = [], []
    tasks = []
    for slug in CONFIG["boards"]["greenhouse"]:
        tasks.append((f"greenhouse:{slug}", lambda s=slug: fetch_greenhouse(s)))
    for slug in CONFIG["boards"]["lever"]:
        tasks.append((f"lever:{slug}", lambda s=slug: fetch_lever(s)))
    for slug in CONFIG["boards"]["ashby"]:
        tasks.append((f"ashby:{slug}", lambda s=slug: fetch_ashby(s)))
    for slug in CONFIG["boards"].get("smartrecruiters", []):
        tasks.append((f"smartrecruiters:{slug}", lambda s=slug: fetch_smartrecruiters(s)))
    if include_broad:
        b = CONFIG["broad_sources"]
        if b.get("remotive"):
            tasks.append(("remotive", fetch_remotive))
        if b.get("remoteok"):
            tasks.append(("remoteok", fetch_remoteok))
        if b.get("weworkremotely"):
            tasks.append(("weworkremotely", fetch_weworkremotely))
    if include_custom:
        cb = CONFIG.get("custom_boards", {})
        sites = cb.get("sites", {})
        for site, fetch in CUSTOM_FETCHERS.items():
            if not sites.get(site):
                continue
            for q in cb.get("queries", []):
                tasks.append((f"{site}:{q}", lambda f=fetch, s=q: f(s)))
        # meta needs the local Playwright venv — skip quietly where absent (cloud)
        if (sites.get("meta") and cb.get("meta_queries")
                and (BASE / ".venv" / "bin" / "python").exists()):
            tasks.append(("meta", lambda: fetch_meta(cb["meta_queries"])))

    def run_task(task):
        name, fn = task
        try:
            return list(fn()), None
        except Exception as e:
            return [], f"{name}: {e}"

    empties = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        for (name, _fn), (result, err) in zip(tasks, pool.map(run_task, tasks)):
            jobs.extend(result)
            if err:
                errors.append(err)
            elif not result and name.split(":", 1)[0] in (
                    "greenhouse", "lever", "ashby", "smartrecruiters"):
                empties.append(name)   # 0 jobs from a full-listing board = dead slug
    return jobs, errors, empties


def expire_stale(con):
    """Clear jobs that sat unactioned past their review-odds window."""
    exp = CONFIG.get("expiry", {})
    norm = "datetime(replace(substr(first_seen,1,19),'T',' '))"
    for tier, statuses, cutoff, label in (
            ("hot", ("new", "needs_review"),
             f"-{exp.get('hot_hours', 48)} hours", f"{exp.get('hot_hours', 48)}h"),
            ("match", ("new",),
             f"-{exp.get('match_days', 7)} days", f"{exp.get('match_days', 7)}d")):
        marks = ",".join("?" * len(statuses))
        cur = con.execute(
            f"UPDATE jobs SET status='hidden', reason=? WHERE tier=? "
            f"AND status IN ({marks}) AND {norm} < datetime('now', ?)",
            (f"expired unactioned after {label}", tier, *statuses, cutoff))
        if cur.rowcount:
            log(f"expired {cur.rowcount} stale {tier}-tier jobs (>{label})")
    con.commit()


def cmd_run(seed=False):
    con = db_connect()
    first_run = con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    seed = seed or first_run
    expire_stale(con)

    # broad sources and big-tech custom boards are polled less often to be
    # polite to their APIs (custom boards are search pages, not bulk feeds)
    now = time.time()

    def due(key, every_minutes):
        row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return (now - (float(row[0]) if row else 0.0)) >= every_minutes * 60

    broad_due = due("last_broad_poll",
                    CONFIG["broad_sources"]["poll_every_minutes"])
    custom_due = due("last_custom_poll",
                     CONFIG.get("custom_boards", {}).get("poll_every_minutes", 15))

    jobs, errors, empties = collect_jobs(include_broad=broad_due,
                                         include_custom=custom_due)
    for flag, key in ((broad_due, "last_broad_poll"),
                      (custom_due, "last_custom_poll")):
        if flag:
            con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, str(now)))
    for e in errors:
        log(f"WARN source failed: {e}")
    # persistent-empty detection: a one-off 0 is just a quiet board, but a
    # full-listing board that returns 0 for many polls in a row means the slug
    # broke (company changed ATS, etc.). Warn once per episode, not every poll.
    ZERO_WARN_STREAK = 30
    ats_boards = [f"{ats}:{s}" for ats in
                  ("greenhouse", "lever", "ashby", "smartrecruiters")
                  for s in CONFIG["boards"].get(ats, [])]
    srow = con.execute(
        "SELECT value FROM meta WHERE key='board_zero_streak'").fetchone()
    try:
        prev = json.loads(srow[0]) if srow else {}
    except (ValueError, TypeError):
        prev = {}
    empty_set = set(empties)
    streak = {b: (prev.get(b, 0) + 1 if b in empty_set else 0) for b in ats_boards}
    con.execute("INSERT OR REPLACE INTO meta VALUES ('board_zero_streak', ?)",
                (json.dumps(streak),))
    crossed = sorted(b for b in ats_boards if streak[b] == ZERO_WARN_STREAK)
    if crossed:
        log(f"WARN {len(crossed)} board(s) empty for {ZERO_WARN_STREAK}+ polls "
            f"(dead slug? check config): {', '.join(crossed)}")

    inc, exc, boost, loc_re = compile_filters()
    new_jobs, llm_calls = [], 0
    for job in jobs:
        tier = matches(job, inc, exc, boost, loc_re)
        if not tier or not job["url"]:
            continue
        jid = job_id(job)
        if con.execute("SELECT 1 FROM jobs WHERE id=?", (jid,)).fetchone():
            continue
        # collapse big-tech reposts / cross-query duplicates that job_id misses
        # (same role, new url). Seen even if the prior copy was hidden/expired,
        # so we don't re-alert or re-screen the same posting.
        if job["source"] in DEDUP_BY_TITLE and con.execute(
                "SELECT 1 FROM jobs WHERE source=? AND company=? AND title=?",
                (job["source"], job["company"], job["title"])).fetchone():
            continue

        def record_excluded(reason):
            # store excluded jobs as hidden so they are NOT re-screened every
            # poll (that re-spawned the LLM/keychain prompt every minute)
            con.execute(
                "INSERT OR IGNORE INTO jobs (id, source, company, title, "
                "location, url, tier, first_seen, status, reason) VALUES "
                "(?,?,?,?,?,?,?,?, 'hidden', ?)",
                (jid, job["source"], job["company"], job["title"],
                 job["location"], job["url"], tier,
                 datetime.now(timezone.utc).isoformat(), reason))

        # second-stage screen on the full description (new jobs only).
        # get_description covers feed/greenhouse/etc; full_description adds the
        # custom boards (amazon/google/apple) whose desc get_description lacked
        # — WITHOUT this, Amazon jobs skipped screening and passed on title only
        try:
            desc = get_description(job) or full_description(
                job["source"], job["company"], job["url"])
        except Exception as e:
            desc = ""
            log(f"WARN desc fetch failed {job['company']}: {e}")
        if desc:
            verdict, why = screen_description(desc)
            if verdict == "exclude":
                log(f"DESC-SKIP {job['company']}: {job['title']} — {why}")
                record_excluded(f"desc-screen: {why}")
                continue
            if verdict == "promote" and tier == "match":
                log(f"DESC-PROMOTE {job['company']}: {job['title']} — {why}")
                tier = "hot"
            # third stage: LLM judge for seniority the regexes can't word-match
            if llm_calls < CONFIG.get("llm_screen", {}).get("max_per_run", 10):
                llm_calls += 1
                lv, lwhy = llm_screen(job["title"], desc)
                if lv == "exclude":
                    log(f"LLM-SKIP {job['company']}: {job['title']} — {lwhy}")
                    record_excluded(f"llm-screen: {lwhy}")
                    continue
        job["tier"] = tier
        hr = company_key(job["company"]) in HAND_REVIEW
        status = "needs_review" if hr else "new"
        reason = "high-profile — review & submit" if hr else None
        con.execute(
            "INSERT INTO jobs (id, source, company, title, location, url, tier, "
            "first_seen, status, reason, posted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (jid, job["source"], job["company"], job["title"], job["location"],
             job["url"], tier, datetime.now(timezone.utc).isoformat(),
             status, reason, job.get("posted_at")))
        new_jobs.append(job)
    con.commit()

    scanned = len(jobs)
    if seed:
        log(f"seeded: scanned {scanned} postings, stored {len(new_jobs)} matches (no notifications)")
        con.close()
        return

    # early-career roles notify, and so does ANY match at a hand-review
    # company — those are the postings where being early matters most;
    # remaining full-time matches land silently in the dashboard
    def is_hr(j):
        return company_key(j["company"]) in HAND_REVIEW
    alert_jobs = [j for j in new_jobs if j["tier"] == "hot" or is_hr(j)]
    if alert_jobs:
        cap = CONFIG["notify"]["max_individual_notifications"]
        if len(alert_jobs) <= cap:
            for j in alert_jobs:
                flag, kind = (("⭐", "hand-review") if is_hr(j)
                              else ("🔥", "early-career"))
                notify_macos(f"{flag} New {kind} role: {j['company']}",
                             f"{j['title']} — {j['location'] or 'n/a'}")
        else:
            notify_macos("JobScout",
                         f"{len(alert_jobs)} new roles — see localhost:8765")
        try:
            notify_email(alert_jobs)
        except Exception as e:
            log(f"WARN email failed: {e}")
        notify_ntfy(alert_jobs)
    for j in new_jobs:
        log(f"NEW [{j['tier']}] {j['company']}: {j['title']} ({j['location']}) {j['url']}")
    log(f"run complete: scanned {scanned}, new {len(new_jobs)} "
        f"({len(alert_jobs)} notified), source errors {len(errors)}")
    con.close()

    venv_py = BASE / ".venv" / "bin" / "python"
    # grade readiness of the freshly-found jobs in the background (detached)
    if new_jobs and venv_py.exists():
        try:
            logf = open(BASE / "logs" / "precheck.log", "a")
            subprocess.Popen(
                [str(venv_py), str(BASE / "auto_apply.py"),
                 "--precheck", "--all", "--workers=3"],
                cwd=str(BASE), stdout=logf, stderr=logf, start_new_session=True)
        except Exception as e:
            log(f"WARN precheck spawn failed: {e}")

    # reconcile: mark jobs whose confirmation email has arrived (cheap IMAP read)
    if venv_py.exists():
        try:
            logf = open(BASE / "logs" / "reconcile.log", "a")
            subprocess.Popen(
                [str(venv_py), str(BASE / "auto_apply.py"), "--reconcile"],
                cwd=str(BASE), stdout=logf, stderr=logf, start_new_session=True)
        except Exception as e:
            log(f"WARN reconcile spawn failed: {e}")


def cmd_cloud():
    """Poll from GitHub Actions while the Mac may be asleep: JSON seen-set
    instead of sqlite, ntfy pushes only, ignores poll_window (the whole point
    is covering overnight/lid-closed hours). First run seeds silently."""
    state_path = Path(os.environ.get("JOBSCOUT_STATE",
                                     str(BASE / "cloud_state.json")))
    state = (json.loads(state_path.read_text())
             if state_path.exists() else {})
    seen_list = state.get("seen", [])
    seen = set(seen_list)
    first_run = not seen

    jobs, errors, _empties = collect_jobs(include_broad=False, include_custom=True)
    for e in errors:
        log(f"WARN source failed: {e}")

    inc, exc, boost, loc_re = compile_filters()
    new_jobs, llm_calls = [], 0
    for job in jobs:
        tier = matches(job, inc, exc, boost, loc_re)
        if not tier or not job["url"]:
            continue
        jid = job_id(job)
        if jid in seen:
            continue
        seen.add(jid)
        seen_list.append(jid)
        try:
            desc = get_description(job)
        except Exception:
            desc = ""
        if desc:
            if screen_description(desc)[0] == "exclude":
                continue
            # LLM judge runs in cloud too when ANTHROPIC_API_KEY is set
            if llm_calls < CONFIG.get("llm_screen", {}).get("max_per_run", 10):
                llm_calls += 1
                if llm_screen(job["title"], desc)[0] == "exclude":
                    continue
        job["tier"] = tier
        new_jobs.append(job)

    if new_jobs and not first_run:
        os.environ["JOBSCOUT_CLOUD"] = "1"
        notify_ntfy(new_jobs)
        try:
            notify_email(new_jobs)   # 24/7 email digest (send_from='cloud')
        except Exception as e:
            log(f"WARN cloud email failed: {e}")
    state_path.write_text(json.dumps(
        {"seen": seen_list[-8000:]}))  # cap growth; drops oldest first
    log(f"cloud run: scanned {len(jobs)}, new {len(new_jobs)}"
        f"{' (seeded silently)' if first_run else ''}, "
        f"source errors {len(errors)}")


def full_description(source, company, url):
    """Best-effort full description for a stored job (has url, not ext_id).
    Covers every source; '' when unavailable."""
    try:
        if source == "amazon":
            # per-job .json is 406-blocked; the HTML posting page carries the
            # full description + qualifications (search-feed teaser did not)
            html_txt = strip_html(get(url, headers=BROWSER_UA).decode(
                "utf-8", "replace"))
            i = html_txt.lower().find("basic qualifications")
            if i < 0:
                i = html_txt.lower().find("qualifications")
            return html_txt[i:i + 4000] if i >= 0 else html_txt[:4000]
        if source == "greenhouse":
            m = re.search(r"(\d{6,})", url)
            if not m:
                return ""
            d = get_json("https://boards-api.greenhouse.io/v1/boards/"
                         f"{company}/jobs/{m.group(1)}")
            return strip_html(html.unescape(d.get("content", "")))
        if source == "netflix":
            return get_description({"source": "netflix", "url": url})
        if source == "apple":
            return strip_html(get(url, headers=BROWSER_UA).decode("utf-8", "replace"))
        if source == "google":
            raw = get(url, headers=BROWSER_UA).decode()
            m = GOOGLE_DATA_RE.search(raw)
            if m:
                rec = json.loads(m.group(1))[0][0]
                return strip_html(" ".join((f or [None, ""])[1] or ""
                                           for f in (rec[3], rec[4])))
        if source == "lever":
            m = re.search(r"jobs\.lever\.co/[^/]+/([0-9a-f-]{36})", url)
            if m:
                d = get_json(f"https://api.lever.co/v0/postings/{company}/{m.group(1)}")
                return strip_html(d.get("descriptionPlain", ""))
        if source == "ashby":
            d = get_json("https://api.ashbyhq.com/posting-api/job-board/"
                         f"{company}")
            for j in d.get("jobs", []):
                if (j.get("jobUrl") or "").rstrip("/").split("?")[0] == \
                        url.rstrip("/").split("?")[0]:
                    return strip_html(j.get("descriptionPlain", ""))
    except Exception:
        return ""
    return ""


RANK_PROMPT = """You are helping Justin decide which jobs to actually apply \
to. Applying to many near-identical roles at one company looks scattershot, \
so he wants ALL of them ranked best-to-worst by fit — where he's both \
competitive AND genuinely well-matched.

Justin: Cornell undergrad, B.S. Biometry & Statistics (expected May 2028), \
~1 year as a data-analytics intern. Strengths: SQL, statistics, analytics, \
dashboards, experimentation. Seeking internships / entry-level data & \
analytics roles. Weaker fit: heavy software/ML-infra engineering, deep \
domain specialties, roles clearly wanting years of experience.

Below are {n} postings (numbered). Return ONLY a JSON array ranking ALL {n} \
of them, best fit first, every posting appearing exactly once:
[{{"n": <number>, "score": <0-100 fit>, "why": "<=15 words"}}]

Postings:
"""


def cmd_rank(company_filter=None, top=3):
    """LLM-rank the visible jobs (optionally one company) by fit for Justin,
    store fit_rank/fit_note, and print the top picks. Runs wherever an
    LLM backend is reachable (API key anywhere, or local claude CLI)."""
    con = db_connect()
    for col in ("fit_rank INTEGER", "fit_note TEXT"):
        try:
            con.execute(f"ALTER TABLE jobs ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    sql = "SELECT id, source, company, title, url FROM jobs WHERE status IN ('new','needs_review')"
    params = []
    if company_filter:
        sql += " AND lower(company)=?"
        params.append(company_filter.lower())
    rows = con.execute(sql, params).fetchall()
    if not rows:
        log(f"rank: no visible jobs{' for ' + company_filter if company_filter else ''}")
        con.close()
        return
    log(f"rank: fetching {len(rows)} descriptions…")
    cands = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        descs = pool.map(lambda r: full_description(r[1], r[2], r[4]), rows)
    for r, d in zip(rows, descs):
        cands.append((r[0], r[3], d))

    listing = "\n\n".join(
        f"[{i+1}] {title}\n{(desc or '(no description)')[:900]}"
        for i, (_, title, desc) in enumerate(cands))
    prompt = RANK_PROMPT.format(n=len(cands)) + listing
    try:
        raw = llm_complete(prompt, max_tokens=400 + 45 * len(cands))
        m = re.search(r"\[.*\]", raw, re.S)
        picks = json.loads(m.group(0)) if m else []
    except Exception as e:
        log(f"rank: LLM error — {e}")
        picks = []
    # fail-safe: never wipe existing ranks on an empty/failed result
    if not picks:
        log("rank: no picks parsed — keeping existing ranks unchanged")
        con.close()
        return

    # clear old ranks for this scope, then write the full ordering
    con.execute("UPDATE jobs SET fit_rank=NULL, fit_note=NULL "
                "WHERE status IN ('new','needs_review')"
                + (" AND lower(company)=?" if company_filter else ""), params)
    # low-fit jobs beyond the top 3 get auto-hidden to keep the bin focused
    # (reversible from the Hidden tab); top 3 always stay visible
    threshold = CONFIG.get("rank", {}).get("hide_below_score", 60)
    print(f"\nAll {len(cands)} ranked by fit"
          f"{' at ' + company_filter if company_filter else ''} "
          "(🎯 = top 3):\n")
    # auto-hide only declutters LARGE pools; for a handful of jobs, keep them
    # all visible (hiding 2 of 5 is just annoying) and let the badges guide
    declutter = len(picks) > CONFIG.get("rank", {}).get("hide_min_pool", 8)
    hidden = 0
    for rank, p in enumerate(picks, 1):
        idx = p.get("n", 0) - 1
        if not (0 <= idx < len(cands)):
            continue
        jid, title, _ = cands[idx]
        try:
            score = int(p.get("score", 0))
        except (TypeError, ValueError):
            score = 0
        note = f"{score}/100 — {p.get('why', '')}"
        con.execute("UPDATE jobs SET fit_rank=?, fit_note=? WHERE id=?",
                    (rank, note, jid))
        if declutter and rank > 3 and score < threshold:
            con.execute("UPDATE jobs SET status='hidden', "
                        "reason=? WHERE id=? AND status IN ('new','needs_review')",
                        (f"low fit ({score}/100) — ranked out", jid))
            hidden += 1
            tag = "hide"
        else:
            tag = "🎯" if rank <= 3 else "keep"
        print(f"  {tag:>4} {rank}. {title}\n        {note}\n")
    con.commit()
    con.close()
    log(f"rank: ranked {len(picks)} jobs; hid {hidden} low-fit "
        f"(score<{threshold}, beyond top 3)")


def cmd_list(n=20):
    con = db_connect()
    rows = con.execute(
        "SELECT first_seen, tier, company, title, location, url FROM jobs "
        "ORDER BY first_seen DESC LIMIT ?", (n,)).fetchall()
    for seen, tier, company, title, location, url in rows:
        hot = "🔥" if tier == "hot" else "  "
        print(f"{seen[:16]} {hot} {company:<16} {title}  [{location}]\n{'':22}{url}")
    con.close()


def within_poll_window():
    """True if the local hour is inside the configured active window. Postings
    appear on a human schedule, so we skip the quiet overnight hours."""
    w = CONFIG.get("poll_window", {})
    start, end = w.get("start_hour", 0), w.get("end_hour", 24)
    hour = time.localtime().tm_hour  # machine-local time
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end  # window wrapping past midnight


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "run"
    if cmd == "run":
        # skip the scheduled poll outside active hours; --force / --seed override
        if ("--force" not in args and "--seed" not in args
                and not within_poll_window()):
            return
        cmd_run(seed="--seed" in args)
    elif cmd == "cloud":
        cmd_cloud()
    elif cmd == "rank":
        company = None
        top = 3
        for a in args[1:]:
            if a.startswith("--company="):
                company = a.split("=", 1)[1]
            elif a.startswith("--top="):
                top = int(a.split("=", 1)[1])
        cmd_rank(company_filter=company, top=top)
    elif cmd == "list":
        cmd_list(int(args[1]) if len(args) > 1 else 20)
    elif cmd == "test-notify":
        notify_macos("JobScout test", "Notifications are working 🎉")
        test_job = {"company": "TestCo", "title": "Data Analyst",
                    "location": "Remote", "url": "https://example.com", "tier": "hot"}
        notify_email([test_job])
        notify_ntfy([test_job])
        print("test notification sent")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
