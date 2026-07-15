#!/usr/bin/env python3
"""JobScout zero-touch auto-apply.

Given a job (by DB id or URL), opens the application form headlessly, fills
every field it can from profile.json + answers.json, attaches the resume
(and transcript where asked), and submits — but ONLY if every required field
was confidently filled and no CAPTCHA is present. Anything uncertain is
queued as 'needs_review' instead, with a screenshot and reason.

Usage:
  .venv/bin/python auto_apply.py <job_db_id | url>            # real submit
  .venv/bin/python auto_apply.py <job_db_id | url> --dry-run  # everything except the submit click
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
PROFILE = json.loads((BASE / "profile.json").read_text())
ANSWERS = json.loads((BASE / "answers.json").read_text())
CONFIG = json.loads((BASE / "config.json").read_text())
EDU = ANSWERS.get("education", {})
# field_of_study is a preference list; first entry is the free-text answer
FOS = EDU.get("field_of_study") or [""]
if isinstance(FOS, str):
    FOS = [FOS]
DB_PATH = BASE / "jobs.db"
SHOTS = BASE / "logs" / "apps"
APP_LOG = BASE / "logs" / "applications.log"

BROWSER_ARGS = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                "--disable-extensions"]


def launch_args(assist=False):
    """Chromium launch flags. Assisted (visible) mode opens the window at the
    top-left so it sits on-screen."""
    args = list(BROWSER_ARGS)
    if assist:
        args += ["--window-position=0,0"]
    return args

# assisted-submit: how long to keep the visible browser open waiting for the
# human to clear a verification gate (email code / CAPTCHA), and poll cadence.
ASSIST_TIMEOUT_S = 300
REVIEW_TIMEOUT_S = 1200   # high-profile review+submit — allow time to read/tailor
ASSIST_POLL_MS = 2000

# full phrases only — bare words ("thank", "received") false-positive on
# posting-page marketing copy and mark jobs applied that never were
OK_MARKERS = ("thank you for applying", "thanks for applying",
              "thank you for your application",
              "application has been received", "application received",
              "application complete", "application submitted",
              "application was submitted", "successfully submitted",
              "we have received your application",
              "we've received your application",
              "we've got your application")


# page states that mean "stop, don't fill": the posting is gone, or the
# site has bot-flagged this network (job still alive — retry later)
DEAD_MARKERS = ("can't find that page", "cannot find that page",
                "job not found", "posting not found",
                "no longer accepting applications",
                "this job is no longer available",
                "position has been filled", "posting has closed",
                "this position is no longer open")
BLOCK_MARKERS = ("access is temporarily restricted",
                 "we detected unusual activity",
                 "verify you are human", "are you a robot")
# OAuth / SSO sign-in walls (Google/Apple/LinkedIn/Amazon): providers block
# sign-in from automated browsers by design — cannot and should not be
# scripted around. These MUST be done by a human in a normal browser.
SSO_MARKERS = ("sign in with a supported browser", "couldn't sign you in",
               "couldn't sign in", "this browser or app may not be secure",
               "controlled through software automation",
               "use a supported browser")


def submission_confirmed(page):
    """True once the page shows an application-received confirmation."""
    try:
        body = page.inner_text("body").lower()
    except Exception:
        return False
    return any(m in body for m in OK_MARKERS)


# ------------------------------------------------------------ auto verification code
# EXPERIMENTAL: reads the one-time code from a dedicated inbox and types it in.
# Off until config.code_inbox.enabled and the password env var are set.

def _imap_password():
    """App password from the env var, else a local file (chmod 600)."""
    c = CONFIG.get("code_inbox", {})
    pw = os.environ.get(c.get("password_env", ""), "")
    if not pw:
        pf = c.get("password_file")
        if pf:
            p = Path(pf) if os.path.isabs(pf) else (BASE / pf)
            try:
                pw = p.read_text()
            except Exception:
                pw = ""
    return "".join(pw.split())  # Gmail shows app pwds with spaces; strip them all


def code_inbox_enabled():
    c = CONFIG.get("code_inbox", {})
    return bool(c.get("enabled") and c.get("user") and _imap_password())


def _extract_code(text):
    """Pull a plausible verification code from email text. Requires a digit (or
    an ALL-CAPS token next to a keyword) so words like 'Google'/'Security' don't
    get mistaken for a code."""
    if not text:
        return None
    # 1) a token right after a code keyword
    for m in re.finditer(
            r"(?:verification|security|one[\s-]?time|confirm(?:ation)?|access|your)"
            r"[^\n:]{0,20}?\b([A-Z0-9]{4,10})\b", text, re.I):
        cand = m.group(1)
        if any(ch.isdigit() for ch in cand) or cand.isupper():
            return cand
    # 2) a standalone token that contains a digit (typical of codes)
    for m in re.finditer(r"\b([A-Z0-9]{5,10})\b", text):
        if any(ch.isdigit() for ch in m.group(1)):
            return m.group(1)
    # 3) plain N-digit code
    m = re.search(r"\b(\d{4,8})\b", text)
    return m.group(1) if m else None


def fetch_verification_code(since_epoch=0):
    """Return the newest verification code from the dedicated inbox, or None.
    Only considers messages newer than since_epoch (the moment we submitted)."""
    c = CONFIG.get("code_inbox", {})
    pw = _imap_password()
    if not (c.get("enabled") and c.get("imap_host") and c.get("user") and pw):
        return None
    import imaplib
    import email
    from email.utils import parsedate_to_datetime
    try:
        M = imaplib.IMAP4_SSL(c["imap_host"], c.get("imap_port", 993))
        M.login(c["user"], pw)
        M.select("INBOX")
        _, data = M.search(None, "ALL")
        best = None
        for i in reversed(data[0].split()[-8:]):  # newest few
            _, md = M.fetch(i, "(RFC822)")
            msg = email.message_from_bytes(md[0][1])
            try:
                ts = parsedate_to_datetime(msg.get("Date")).timestamp()
            except Exception:
                ts = 0
            if since_epoch and ts and ts < since_epoch:
                continue
            body = str(msg.get("Subject", "")) + "\n"
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        body += part.get_payload(decode=True).decode(errors="ignore")
            else:
                body += msg.get_payload(decode=True).decode(errors="ignore")
            code = _extract_code(body)
            if code and (best is None or ts > best[1]):
                best = (code, ts)
        M.logout()
        return best[0] if best else None
    except Exception as e:
        log(f"code-inbox error: {e}")
        return None


def enter_verification_code(page, code):
    """Best-effort: type the code into the security-code field(s) and submit."""
    try:
        boxes = page.locator(
            "input[autocomplete='one-time-code'], input[name*='code' i], "
            "input[aria-label*='code' i], input[placeholder*='code' i]")
        n = boxes.count()
        if n == 0:
            return False
        if n >= len(code):                 # one box per character
            for idx, ch in enumerate(code):
                boxes.nth(idx).fill(ch)
        else:                              # single field
            boxes.first.fill(code)
        page.wait_for_timeout(600)
        submit = page.locator(
            "button[type='submit'], button:has-text('Submit'), "
            "button:has-text('Verify'), button:has-text('Confirm')")
        if submit.count():
            submit.first.click()
        return True
    except Exception:
        return False


def notify_confirmation(company, title):
    """Push (macOS + ntfy) when an application-confirmation email is spotted,
    so Justin hears about it without checking the dedicated inbox."""
    try:
        notify("✅ Application confirmed", f"{company}: {title}")
        ncfg = json.loads((BASE / "config.json").read_text()) \
            .get("notify", {}).get("ntfy", {})
        if ncfg.get("enabled") and ncfg.get("topic"):
            import urllib.request
            req = urllib.request.Request(
                f"https://ntfy.sh/{ncfg['topic']}",
                data=f"{company}: {title}".encode(),
                headers={"Title": "Application confirmed",
                         "User-Agent": "jobscout-personal/1.0"})
            urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        log(f"WARN confirmation notify failed: {e}")


def reconcile_from_inbox():
    """Read application-confirmation emails from the dedicated inbox and mark the
    matching jobs as submitted. Catches applications that went through but whose
    success the browser automation missed (page navigated/closed on submit)."""
    c = CONFIG.get("code_inbox", {})
    pw = _imap_password()
    if not (c.get("enabled") and c.get("imap_host") and c.get("user") and pw):
        print("reconcile: code_inbox not configured")
        return 0
    import imaplib
    import email
    con = sqlite3.connect(DB_PATH, timeout=30)
    jobs = con.execute(
        "SELECT id, company, title FROM jobs "
        "WHERE status IN ('new','needs_review')").fetchall()
    marked = 0

    def norm(s):
        return " ".join((s or "").split()).lower()  # unfold + collapse whitespace

    def parse_subject(subj):
        """-> (role, company). Confirmations read 'application for <role> at <co>'."""
        subj = " ".join((subj or "").split())        # unfold header line-wraps
        m = re.search(r"application\s+(?:for|to)\s+(.+?)\s+at\s+([\w .,&'-]+?)\s*$",
                      subj, re.I)
        if m:
            return norm(m.group(1)), norm(m.group(2))
        m = re.search(r"application\s+(?:for|to)\s+(.+?)\s*$", subj, re.I)
        return (norm(m.group(1) if m else subj), "")

    def company_from_body(text):
        for pat in (r"joining the\s+([A-Za-z0-9][\w .&'-]{1,30}?)\s+team",
                    r"applying to\s+([A-Za-z0-9][\w .&'-]{1,30})",
                    r"(?:greenhouse|lever|ashbyhq)\.[a-z.]+/(?:embed/job_app\?for=)?"
                    r"([A-Za-z0-9]+)"):
            m = re.search(pat, text, re.I)
            if m:
                return m.group(1).strip().lower()
        return ""

    def email_text(msg):
        parts = [str(msg.get("Subject", ""))]
        if msg.is_multipart():
            for p in msg.walk():
                if p.get_content_type() in ("text/plain", "text/html"):
                    try:
                        parts.append(p.get_payload(decode=True).decode(errors="ignore"))
                    except Exception:
                        pass
        else:
            try:
                parts.append(msg.get_payload(decode=True).decode(errors="ignore"))
            except Exception:
                pass
        return "\n".join(parts)

    try:
        M = imaplib.IMAP4_SSL(c["imap_host"], c.get("imap_port", 993))
        M.login(c["user"], pw)
        M.select("INBOX")
        _, data = M.search(None, "ALL")
        now = datetime.now(timezone.utc).isoformat()
        for i in reversed(data[0].split()[-40:]):
            _, md = M.fetch(i, "(RFC822)")
            msg = email.message_from_bytes(md[0][1])
            subj = str(msg.get("Subject", ""))
            if not re.search(r"appl(ication|ied|y)|thank you for applying", subj, re.I):
                continue
            role, ecomp = parse_subject(subj)
            if not ecomp:
                ecomp = company_from_body(email_text(msg))
            # best match: same company AND strong title overlap (longest wins)
            best = None
            for jid, comp, title in jobs:
                cl = (comp or "").lower()
                tl = norm(title)
                if not tl:
                    continue
                comp_ok = bool(ecomp) and (ecomp == cl or ecomp in cl or cl in ecomp)
                if not comp_ok:
                    continue
                # require the full role/title to line up, not a generic substring
                if role.startswith(tl) or tl.startswith(role) or role == tl:
                    if best is None or len(tl) > len(best[2]):
                        best = (jid, comp, tl, title)
            if best:
                cur = con.execute(
                    "UPDATE jobs SET status='applied', reason='confirmed by email', "
                    "applied_at=? WHERE id=? AND status IN ('new','needs_review')",
                    (now, best[0]))
                if cur.rowcount:
                    marked += 1
                    log(f"RECONCILE: {best[1]}: {best[3]} -> confirmed by email")
                    notify_confirmation(best[1], best[3])
        con.commit()
        M.logout()
    except Exception as e:
        log(f"reconcile error: {e}")
    finally:
        con.close()
    log(f"reconcile: marked {marked} job(s) as submitted from inbox confirmations")
    return marked


def classify_readiness(report, blockers):
    """Turn a fill result into a triage verdict for the dashboard badge.
    ready = fills clean; captcha = interactive challenge; missing-info = a
    required field we can't answer; form-issue = form didn't load."""
    if not (report["filled"] or report["chosen"] or report["files"]):
        return "form-issue"
    joined = " ".join(blockers).lower()
    if "captcha" in joined:
        return "captcha"
    if blockers:
        return "missing-info"
    return "ready"


def detected_gate(page):
    """Describe the human-verification step blocking submission, for the prompt."""
    try:
        body = page.inner_text("body").lower()
    except Exception:
        body = ""
    if "verification code" in body or "8-character code" in body or \
            "confirm you're a human" in body or "security code" in body:
        return "email verification code"
    if page.locator(".h-captcha, iframe[src*='hcaptcha'], .cf-turnstile, "
                    ".g-recaptcha").count():
        return "CAPTCHA challenge"
    return "final human check"


def log(msg):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line)
    with open(APP_LOG, "a") as f:
        f.write(line + "\n")


def notify(title, message):
    script = (f'display notification "{message.replace(chr(34), chr(39))}" '
              f'with title "{title.replace(chr(34), chr(39))}" sound name "Glass"')
    subprocess.run(["osascript", "-e", script], capture_output=True)


# ------------------------------------------------------------ answer logic

TEXT_FIELDS = [
    (["first name", "first_name", "firstname", "given"], PROFILE["first_name"]),
    (["last name", "last_name", "lastname", "family"], PROFILE["last_name"]),
    (["full name", "your name", "legal name", "name"], PROFILE["full_name"]),
    (["email"], PROFILE["email"]),
    (["phone"], PROFILE["phone"]),
    (["linkedin"], PROFILE["linkedin"]),
    (["github", "portfolio", "website"], PROFILE["github"]),
    # no bare "location" — it substring-matches "relocation"
    (["current location", "location (city)", "your location", "city"],
     PROFILE["location"]),
    (["school", "university", "college"], PROFILE.get("school", "")),
    (["discipline", "field of study", "major", "concentration",
      "course of study", "area of study"], FOS[0]),
    (["degree"], PROFILE.get("degree", "")),
    (["graduation", "grad date"], ANSWERS["graduation_date"]),
    (["start date", "earliest start", "when can you start",
      "availability date", "date available"], ANSWERS["earliest_start"]),
    (["how did you hear", "hear about"], ANSWERS["how_did_you_hear"]),
    # grad-level GPA/GRE asked of everyone on some forms — N/A for an undergrad
    (["gpa (graduate", "gpa (doctorate", "gpa (masters", "graduate gpa",
      "doctorate gpa"], "N/A"),
    (["gre score"], "N/A"),
    (["gpa"], ANSWERS["gpa"]),
    (["sat score", "sat total"], ANSWERS.get("sat_score", "")),
    (["salary", "compensation"], ANSWERS["salary_expectation"]),
    (["country code"], "+1"),
    (["state of residence", "current state"], ANSWERS["location"]["state"]),
    (["current or most recent employer", "most recent employer",
      "current employer", "current company", "last company"],
     ANSWERS["employment"]["current_company"]),
    (["recent job title", "current job title", "current title",
      "most recent title"], ANSWERS["employment"]["current_title"]),
    (["sponsorship", "sponsor"], "No"),  # before work-authorization: collide
    (["work authorization"],
     "U.S. citizen — authorized to work in the U.S.; no sponsorship required"),
    (["employment history", "history with"], "None"),
]

# question-text fragment -> option-text fragments we accept, in preference order
CHOICE_RULES = [
    (["sponsor"], ["no"]),
    (["authorized to work", "legally authorized", "work authorization",
      "eligible to work"], ["yes"]),
    (["hispanic", "latino"], ["no"]),
    (["gender"], [ANSWERS["eeo"]["gender"], "man"]),
    # "asian" fragment covers per-checkbox EEO groups where each option is its
    # own labeled field; "not hispanic" covers combined ethnicity lists
    (["race", "ethnic", "asian"],
     [ANSWERS["eeo"]["race"], "not hispanic"]),
    (["veteran"], ["i am not a protected veteran", "not a veteran", "no"]),
    (["disability", "disabled"], ["no, i do not", "i do not have a disability",
                                  "i don't wish to answer", "no"]),
    (["how did you hear", "hear about"], ["careers page", "company website",
                                          "job board", "other"]),
    (["previously worked", "previously been employed",
      "worked for", "former employee", "current or former",
      "employment history", "history with"], ["no", "never", "none"]),
    (["interviewed"], ["no"]),
    (["relocat"], ["yes"]),
    (["in-person", "in person", "hybrid", "onsite", "on-site"], ["yes"]),
    (["export control", "u.s. person", "us person", "itar"],
     ["yes", "u.s. citizen", "us citizen"]),
    (["security clearance", "clearance level"],
     ["no", "none", "never held", "i have not"]),
    (["citizenship status", "citizenship"], ["u.s. citizen", "yes"]),
    (["18 years", "age of 18", "at least 18"], ["yes"]),
    (["degree", "education level", "level of education", "highest degree",
      "degree type", "degree level", "which degree"], ["bachelor"]),
    (["privacy", "consent", "acknowledge", "agree", "terms"], ["yes", "i agree",
                                                               "i acknowledge",
                                                               "accept"]),
]


def pick_option(question, options):
    """Return the option text to choose for a question, or None."""
    q = question.lower()
    for fragments, wanted in CHOICE_RULES:
        if any(f in q for f in fragments):
            # exact match first — "man" must select "Man", never hit "Woman"
            for w in wanted:
                for opt in options:
                    if opt.lower().strip() == w:
                        return opt
            for w in wanted:
                for opt in options:
                    # short wants match whole words only — "no" must never
                    # hit "Latino" or "Norway"
                    if len(w) <= 3:
                        if re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])",
                                     opt.lower()):
                            return opt
                    elif w in opt.lower():
                        return opt
    return None


# For type-to-filter dropdown widgets (Greenhouse/Ashby react-select): map a
# question to the SEARCH STRING to type. Only safe, standard questions with an
# unambiguous answer are listed — anything else is left for manual review.
# Each value is a list of acceptable answers in preference order; the first
# that matches an actual option on the widget wins.
SELECT_ANSWERS = [
    # sponsor MUST precede authorization: "require sponsorship for work
    # authorization?" contains both fragments, and the answers are opposite
    (["sponsor", "immigration"], ["No, I do", "No", "No,"]),
    (["authorized to work", "legally authorized", "work authorization",
      "eligible to work", "legally authorised", "authorised to work"],
     ["Yes, no restriction", "Yes", "Yes,"]),
    (["country code"], ["+1", "United States"]),
    (["country"], ["United States"]),
    (["location (city)", "current location"], ["Ithaca", "New York"]),
    (["state", "province", "reside"], ["New York"]),
    (["relocat"], ["Yes"]),
    (["interviewed"], ["No"]),
    (["export control", "u.s. person", "us person"], ["Yes"]),
    (["clearance"], ["No", "None", "I have not"]),
    (["hispanic", "latino"], ["No", "Not Hispanic"]),
    (["gender"], [ANSWERS["eeo"]["gender"], "Male"]),
    (["race", "ethnic"], [ANSWERS["eeo"]["race"], "Asian"]),
    (["veteran"], ["I am not a protected veteran", "not a protected veteran",
                   "not a veteran", "No"]),
    (["disability", "disabled"], ["No, I do", "I do not have a disability",
                                  "No", "I don't wish"]),
    (["pronoun"], ["He/Him", "He / Him", "he/him"]),
    (["how did you hear", "learn about", "hear about"],
     ["LinkedIn", "Company website", "Company Careers", "Job board", "Indeed",
      "Glassdoor", "Other"]),
    (["cities", "which city", "office location", "location preference",
      "available to work"], ["Remote", "New York", "New York City",
                             "No preference", "Open to all", "Any"]),
    (["school", "university", "college", "institution", "did you attend"],
     [EDU.get("school", "Cornell University"), "Cornell University", "Cornell"]),
    (["field of study", "discipline", "major", "concentration",
      "course of study", "area of study"], FOS),
    (["degree"], [EDU.get("degree_level", "Bachelor's Degree"), "Bachelor's Degree",
                  "Bachelor of Science", "Bachelor", "Bachelors"]),
    (["previously", "former employee", "worked for", "been employed"], ["No"]),
    (["18 years", "age of 18", "at least 18"], ["Yes"]),
    (["acknowledge", "consent", "certify", "agree", "understand",
      "i have read", "attest", "confirm"], ["Yes", "I agree", "I acknowledge",
                                            "I certify", "I understand"]),
]


def select_answer(hints):
    for fragments, vals in SELECT_ANSWERS:
        if any(f in hints for f in fragments):
            return vals if isinstance(vals, list) else [vals]
    return None


# Required standalone checkboxes that are truthful acknowledgements the
# applicant would sign anyway. Deliberately excludes marketing/promotional
# opt-ins (those are never auto-checked).
CONSENT_CHECKBOX = ("i certify", "certify that", "true and correct", "attest",
                    "i acknowledge", "acknowledge that", "i understand",
                    "understand that", "i have read", "i agree", "agree to",
                    "i confirm", "confirm that", "privacy policy",
                    "terms and conditions", "processing of my",
                    "data processing", "candidate privacy")


def fill_react_selects(page, report):
    """Fill Greenhouse/Ashby type-to-filter dropdown widgets (react-select),
    which the native <select> pass can't touch. Only standard questions with a
    safe answer are filled; the rest stay empty and surface as review items."""
    # Committing a react-select reflows the form, which invalidates positional
    # locators (.nth(i)) mid-loop — so re-scan and process ONE new shell per
    # pass, tracked by a stable key, until none remain. Each pass reads every
    # shell's id + label + already-filled flag in a SINGLE round-trip.
    processed = set()
    for _ in range(60):  # hard cap; there are never this many widgets
        meta = page.evaluate("""() => Array.from(document.querySelectorAll('.select-shell')).map(s => {
            const inp = s.querySelector('input');
            const ctrl = s.querySelector('.select__control');
            let lbl=''; let n=s;
            for(let d=0; d<6 && n; d++){ n=n.parentElement; if(!n) break;
              const l=n.querySelector('label,legend'); if(l){lbl=l.textContent.trim(); break;} }
            const filled = ctrl && (ctrl.querySelector('[class*=has-value]')
              || ctrl.querySelector('[class*=single-value],[class*=singleValue],[class*=multiValue]'));
            return {id: inp ? (inp.id||'') : '', label: lbl,
                    visible: inp ? inp.offsetParent !== null : false, filled: !!filled};
        })""")
        target = None
        for i, m in enumerate(meta):
            key = m["id"] or f"_shell{i}"
            if key in processed:
                continue
            processed.add(key)
            target = (i, m)
            break
        if target is None:
            break  # every shell handled
        i, m = target
        if not m["visible"] or m["filled"]:
            continue  # hidden, or already has a value (e.g. phone-country default)
        hints = (m["label"] + " " + m["id"].replace("_", " ")).lower()
        want = select_answer(hints)
        if not want:
            continue
        shell = page.locator(".select-shell").nth(i)
        inp = shell.locator("input").first
        chosen = commit_react_select(page, shell, inp, want)
        if chosen:
            report["chosen"].append(f"{(m['label'] or m['id'])[:50]} = {chosen[:50]}")


def _match_option(options, wants):
    """Pick the correct option for the first candidate in `wants` that matches —
    correctness-first, never a wrong guess. Exact, then prefix; loose substring
    only for long answers (avoids 'No' matching 'Norway')."""
    for want in wants:
        wl = want.lower().strip()
        if not wl:
            continue
        for o in options:               # exact
            if o.strip().lower() == wl:
                return o
        for o in options:               # prefix on a word/clause boundary
            ol = o.strip().lower()
            if ol.startswith(wl + " ") or ol.startswith(wl + ",") \
                    or ol.startswith(wl + "."):
                return o
        if len(wl) >= 6:                # long answer -> substring is safe
            for o in options:
                if wl in o.lower():
                    return o
    return None


def commit_react_select(page, shell, inp, wants):
    """Open the widget, pick the correct option (options scoped to THIS widget's
    react-select menu, not the ever-present phone-country list), and confirm a
    value committed. Returns chosen text or None (left empty for review rather
    than committing a wrong value)."""
    committed_sel = (".select__control [class*='has-value'], "
                     "[class*='single-value'], [class*='multiValue']")
    try:
        inp.scroll_into_view_if_needed()
        # scope options to THIS widget's own menu — a sibling widget's open menu
        # (e.g. the phone-country picker) must not pollute the option list.
        menu = shell.locator(".select__menu [role='option'], "
                             ".select__menu [class*='option']")
        menu_box = shell.locator(".select__menu")
        inp.click()
        # wait for the menu to actually render instead of sleeping a fixed 500ms
        try:
            menu_box.first.wait_for(state="visible", timeout=2500)
        except Exception:
            try:
                shell.locator(".select__control").first.click()
                menu_box.first.wait_for(state="visible", timeout=2000)
            except Exception:
                pass
        read_opts = ("""s => Array.from(s.querySelectorAll(
            '.select__menu [role=option], .select__menu [class*=option]'))
            .map(o => (o.innerText || '').trim()).filter(Boolean).slice(0, 80)""")
        # read ALL options in a single round-trip (was up to 60 round-trips)
        options = shell.evaluate(read_opts)
        target = _match_option(options, wants)
        # async type-to-search widgets (e.g. School) render no options until you
        # type — try each candidate as a search term.
        if target is None:
            for cand in wants[:4]:
                if not cand:
                    continue
                try:
                    inp.fill("")
                    inp.type(cand[:25], delay=15)
                    page.wait_for_timeout(900)
                    options = shell.evaluate(read_opts)
                    target = _match_option(options, [cand] + list(wants))
                    if target:
                        break
                except Exception:
                    pass
        if target is None:
            page.keyboard.press("Escape")
            return None
        menu.nth(options.index(target)).click()
        # wait for the value to commit instead of sleeping a fixed 300ms
        try:
            shell.locator(committed_sel).first.wait_for(state="attached", timeout=1500)
        except Exception:
            pass
        if shell.locator(committed_sel).count() == 0:
            page.keyboard.press("Escape")
            return None
        return target
    except Exception:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return None


# hints that should NEVER receive free-typed text — they're choice/consent
# questions (handled by CHOICE_RULES) and mis-typing them corrupts the app.
NEVER_TEXT = ("hispanic", "latino", "race", "ethnic", "gender", "veteran",
              "disability", "pronoun", "pronunciation", "sponsor", "authoriz",
              "authorised", "citizen", "consent", "acknowledge", "do you agree",
              "years of experience", "years experience")


def text_answer(hints):
    if any(n in hints for n in NEVER_TEXT):
        return None
    for fragments, value in TEXT_FIELDS:
        if value and any(f in hints for f in fragments):
            if "gpa" in fragments and ANSWERS["gpa_policy"] == "required_only":
                return value if "required" in hints or "*" in hints else None
            return value
    return None


# ------------------------------------------------------------ page driving

def element_hints(el):
    parts = []
    for attr in ("name", "id", "placeholder", "aria-label", "autocomplete",
                 "aria-required", "required"):
        try:
            v = el.get_attribute(attr)
        except Exception:
            v = None
        if v is not None:
            parts.append(f"{attr}:{v}" if attr in ("aria-required", "required") else v)
    try:
        label = el.evaluate("""e => {
            if (e.labels && e.labels.length) return e.labels[0].textContent;
            const c = e.closest('label'); if (c) return c.textContent;
            const w = e.closest('div,fieldset');
            if (w) { const l = w.querySelector('label,legend'); if (l) return l.textContent; }
            return '';
        }""") or ""
    except Exception:
        label = ""
    hints = (" ".join(parts) + " " + label).lower()
    if "aria-required:true" in hints or "required:" in hints or "*" in label:
        hints += " required"
    return hints, label.strip()


def fill_page(page):
    report = {"filled": [], "chosen": [], "files": [], "skipped": []}

    # -- text inputs & textareas
    for sel in ("input[type='text'], input[type='email'], input[type='tel'], "
                "input[type='number'], input:not([type])", "textarea"):
        els = page.locator(sel)
        for i in range(els.count()):
            el = els.nth(i)
            try:
                if not el.is_visible() or el.input_value():
                    continue
            except Exception:
                continue
            hints, label = element_hints(el)
            val = text_answer(hints)
            if val:
                try:
                    el.fill(val)
                    report["filled"].append(f"{(label or hints)[:60]} = {val[:40]}")
                except Exception:
                    pass

    # -- native selects
    sels = page.locator("select")
    for i in range(sels.count()):
        el = sels.nth(i)
        try:
            if not el.is_visible():
                continue
            if el.evaluate("e => e.selectedIndex > 0"):
                continue
        except Exception:
            continue
        hints, label = element_hints(el)
        options = el.evaluate(
            "e => Array.from(e.options).map(o => o.textContent.trim())")
        choice = pick_option(hints, options)
        if choice:
            try:
                el.select_option(label=choice)
                report["chosen"].append(f"{(label or hints)[:60]} = {choice[:50]}")
            except Exception:
                pass

    # -- type-to-filter dropdown widgets (react-select) that native <select>
    #    handling can't reach (Greenhouse/Ashby Country, EEO, sponsorship, …)
    fill_react_selects(page, report)

    # -- radio groups & standalone checkboxes
    radios = page.locator("input[type='radio'], input[type='checkbox']")
    seen_groups = set()
    for i in range(radios.count()):
        el = radios.nth(i)
        try:
            if not el.is_visible() or el.is_checked():
                continue
        except Exception:
            continue
        name = el.get_attribute("name") or f"_solo{i}"
        if name in seen_groups:
            continue
        hints, _ = element_hints(el)
        # Ashby uses the literal question text as the name attribute, so it can
        # contain quotes ("Plaid's Mission") — escape for the CSS string
        css_name = name.replace("\\", "\\\\").replace('"', '\\"')
        group = page.locator(f'input[name="{css_name}"]') if not name.startswith("_solo") else el
        opts, opt_els = [], []
        for k in range(group.count() if not name.startswith("_solo") else 1):
            g = group.nth(k) if not name.startswith("_solo") else el
            _, glabel = element_hints(g)
            opts.append(glabel)
            opt_els.append(g)
        choice = pick_option(hints, opts)
        # Standalone required certification/consent checkbox (label == the
        # statement, no yes/no options): tick it. These are truthful
        # acknowledgements the applicant would sign anyway.
        is_solo_checkbox = (el.get_attribute("type") == "checkbox"
                            and (name.startswith("_solo") or group.count() == 1))
        if choice is None and is_solo_checkbox and any(
                k in hints for k in CONSENT_CHECKBOX):
            try:
                el.check()
                report["chosen"].append(f"[consent] {hints[:70]} = checked")
                seen_groups.add(name)
            except Exception:
                pass
            continue
        if choice is not None:
            try:
                opt_els[opts.index(choice)].check()
                report["chosen"].append(f"{hints[:60]} = {choice[:50]}")
                seen_groups.add(name)
            except Exception:
                pass

    # -- file uploads: resume first, transcript where a second slot asks
    files = page.locator("input[type='file']")
    for i in range(files.count()):
        el = files.nth(i)
        hints, label = element_hints(el)
        path = None
        if any(w in hints for w in ("transcript",)):
            path = ANSWERS["transcript_path"]
        elif any(w in hints for w in ("resume", "cv")) or i == 0:
            path = PROFILE["resume_path"]
        if path and Path(path).exists():
            try:
                el.set_input_files(path)
                report["files"].append(f"{(label or 'file')[:40]} <- {Path(path).name}")
            except Exception:
                pass
    return report


def find_blockers(page):
    """Return list of reasons this application can't be safely submitted."""
    reasons = []
    # hCaptcha / Cloudflare Turnstile are interactive -> always a hard block.
    if page.locator(".h-captcha, iframe[src*='hcaptcha'], .cf-turnstile").count():
        reasons.append("CAPTCHA present (hcaptcha/turnstile)")
    # reCAPTCHA: only the VISIBLE checkbox variant blocks. Invisible v2/v3
    # (used by Greenhouse/Ashby) scores traffic in the background and needs no
    # user interaction, so it should not force every form into manual review.
    else:
        interactive_recaptcha = page.evaluate("""() => {
            const widgets = Array.from(document.querySelectorAll('.g-recaptcha'));
            if (widgets.some(e => (e.getAttribute('data-size') || 'normal') !== 'invisible'))
                return true;
            // a rendered, on-screen checkbox anchor iframe
            return Array.from(document.querySelectorAll("iframe[src*='recaptcha/api2/anchor']"))
                .some(f => f.offsetParent !== null && f.getBoundingClientRect().height > 20);
        }""")
        if interactive_recaptcha:
            reasons.append("CAPTCHA present (interactive reCAPTCHA)")
    empties = page.evaluate("""() => {
        const out = [];
        for (const el of document.querySelectorAll('input,select,textarea')) {
            if (el.type === 'hidden' || el.type === 'file' || el.offsetParent === null) continue;
            const req = el.required || el.getAttribute('aria-required') === 'true';
            if (!req) continue;
            // react-select: the visible search input stays `required` with an
            // empty .value even after a pick — the real signal is the control
            // gaining a --has-value class (or a rendered single/multi value).
            const control = el.closest('.select-shell')?.querySelector('.select__control')
                         || el.closest('.select__control');
            if (control && (control.querySelector('[class*="has-value"]')
                    || control.querySelector('[class*="singleValue"], '
                        + '[class*="single-value"], [class*="multiValue"]'))) {
                continue;
            }
            let empty;
            if (el.type === 'checkbox' || el.type === 'radio') {
                const grp = el.name ? document.querySelectorAll(`input[name="${el.name}"]`) : [el];
                empty = !Array.from(grp).some(g => g.checked);
            } else if (el.tagName === 'SELECT') {
                empty = el.selectedIndex <= 0 && !el.value;
            } else {
                empty = !el.value;
            }
            if (empty) {
                const lbl = (el.labels && el.labels[0]?.textContent)
                    || el.getAttribute('aria-label') || el.name || el.id || 'unnamed field';
                out.push(lbl.trim().slice(0, 80));
            }
        }
        return [...new Set(out)];
    }""")
    reasons.extend(f"required field empty: {e}" for e in empties[:8])
    # unfilled required rich widgets (react-select etc.) with no selection yet
    unfilled_combo = page.evaluate("""() => {
        for (const shell of document.querySelectorAll('.select-shell')) {
            const req = shell.querySelector('input[required], input[aria-required="true"]');
            if (!req || req.offsetParent === null) continue;
            const control = shell.querySelector('.select__control');
            const filled = control && (control.querySelector('[class*="has-value"]')
                || control.querySelector('[class*="singleValue"], '
                    + '[class*="single-value"], [class*="multiValue"]'));
            if (!filled) return true;
        }
        return false;
    }""")
    if unfilled_combo:
        reasons.append("required dropdown widget needs manual selection")
    return reasons


def company_throttled(con, company):
    cap = CONFIG.get("auto_apply", {}).get("max_per_company_per_week", 5)
    n = con.execute(
        "SELECT COUNT(*) FROM jobs WHERE company=? AND status='auto_applied' "
        "AND applied_at > datetime('now', '-7 days')", (company,)).fetchone()[0]
    return n >= cap


def hand_review_set():
    return {c.lower() for c in CONFIG.get("auto_apply", {}).get(
        "hand_review_companies", [])}


def is_hand_review(company):
    return (company or "").lower() in hand_review_set()


def config_slug(company, ats):
    """Find the configured board slug for a company on a given ATS."""
    cl = (company or "").lower().replace(" ", "")
    for s in CONFIG.get("boards", {}).get(ats, []):
        if s.lower().replace(" ", "") == cl:
            return s
    return None


def resolve_apply_url(source, company, url):
    """Map a job-posting URL to the actual application FORM url, so we land on
    a fillable form instead of a description page. Returns (form_url, ats).
    ats in {greenhouse, lever, ashby} => form is direct (skip the Apply click);
    anything else => original url, and we fall back to clicking Apply."""
    src = (source or "").lower()

    if src == "greenhouse":
        m = re.search(r"/jobs/(\d+)", url) or re.search(r"gh_jid=(\d+)", url)
        token = m.group(1) if m else None
        slug = None
        m2 = re.search(r"(?:job-boards|boards)\.greenhouse\.io/([^/?#]+)/jobs/", url)
        if m2:
            slug = m2.group(1)
        if not slug:
            slug = config_slug(company, "greenhouse")
        if token and slug:
            # classic embed endpoint: a plain server-rendered application form
            return (f"https://boards.greenhouse.io/embed/job_app?for={slug}"
                    f"&token={token}"), "greenhouse"

    if src == "lever":
        base = url.split("?")[0].rstrip("/")
        if not base.endswith("/apply"):
            base += "/apply"
        return base, "lever"

    if src == "ashby":
        base = url.split("?")[0].rstrip("/")
        if not base.endswith("/application"):
            base += "/application"
        return base, "ashby"

    return url, src  # smartrecruiters / remotive / unknown -> click Apply


# ------------------------------------------------------------ main

def run(job_ref, dry_run=False, browser=None, assist=False, review=False,
        precheck=False):
    visible = assist or review          # a human is watching this window
    con = sqlite3.connect(DB_PATH, timeout=30)  # tolerate concurrent shard writes
    row = con.execute(
        "SELECT id, company, title, url, source FROM jobs WHERE id=? OR url=?",
        (job_ref, job_ref)).fetchone()
    if not row:
        print(f"job not found in db: {job_ref}")
        return 1
    jid, company, title, url, source = row

    def finish(status, reason=""):
        # dry-run never mutates the DB — it only reports what would happen.
        if dry_run:
            con.close()
            return
        con.execute("UPDATE jobs SET status=?, reason=?, applied_at=? WHERE id=?",
                    (status, reason,
                     datetime.now(timezone.utc).isoformat()
                     if status in ("auto_applied", "applied") else None,
                     jid))
        con.commit()
        con.close()

    # High-profile companies are never auto-SUBMITTED. Headless/batch runs park
    # them in Review (pre-filled on demand); a visible review-mode window fills
    # them and lets the user review + submit, so we DON'T skip when visible.
    hand_review = is_hand_review(company)
    # precheck fills high-profile jobs too (to grade readiness) — don't skip them
    if hand_review and not visible and not precheck:
        tag = "DRY-RUN HIGH-PROFILE" if dry_run else "HIGH-PROFILE"
        log(f"{tag} {company}: {title} — routed to review (no auto-submit)")
        finish("needs_review", "high-profile — review & submit")
        if not dry_run:
            notify("JobScout: review & submit ⭐", f"{company} — {title}")
        return 0

    if company_throttled(con, company) and not precheck:
        log(f"THROTTLE {company}: weekly cap reached — queued for review: {title}")
        finish("needs_review", "weekly per-company cap reached")
        if not dry_run:
            notify("JobScout: review needed", f"{company} — {title} (weekly cap)")
        return 0

    from playwright.sync_api import sync_playwright
    SHOTS.mkdir(parents=True, exist_ok=True)
    shot = SHOTS / f"{jid[:12]}.png"

    apply_url, ats = resolve_apply_url(source, company, url)
    direct = ats in ("greenhouse", "lever", "ashby")
    log(f"NAV {company}: {title} -> [{ats}] {apply_url}")

    # Reuse a caller-supplied browser (batch mode) or launch our own (single job).
    own_pw = browser is None
    pw = None
    if own_pw:
        pw = sync_playwright().start()
        # visible (assist/review) mode so the user can review, clear gates, submit
        browser = pw.chromium.launch(headless=not visible,
                                      args=launch_args(visible))
    if visible:
        # screen-height window so the whole form (incl. the code box + Submit
        # at the bottom) is reachable by scrolling. The tall 2000px viewport is
        # only for headless full-page screenshots and runs off-screen if shown.
        context = browser.new_context(viewport={"width": 1280, "height": 820})
    else:
        context = browser.new_context(viewport={"width": 1280, "height": 2000})
    page = context.new_page()
    page.set_default_timeout(8000)            # bound every element op
    page.set_default_navigation_timeout(30000)
    try:
        page.goto(apply_url, wait_until="domcontentloaded", timeout=30000)
        # wait for the application form to actually render (SPA-friendly)
        try:
            page.wait_for_selector(
                "input:not([type=hidden]):visible, textarea:visible, select:visible",
                timeout=15000)
        except Exception:
            pass
        # brief settle; SPAs (Ashby) need a touch more than server-rendered forms
        page.wait_for_timeout(700 if ats == "ashby" else 300)

        # dead posting / bot-block triage — don't fill or sit on these pages
        try:
            body_txt = page.inner_text("body").lower()
        except Exception:
            body_txt = ""
        if any(m in body_txt for m in DEAD_MARKERS):
            log(f"GONE {company}: {title} — posting removed/closed")
            finish("hidden", "posting removed or closed")
            return 0
        if any(m in body_txt for m in BLOCK_MARKERS):
            log(f"BLOCKED {company}: {title} — site is rate-limiting this "
                "network; retry later")
            finish("needs_review", "site bot-blocked automation — retry "
                   "later or apply manually")
            return 0
        if any(m in body_txt for m in SSO_MARKERS):
            log(f"SSO-WALL {company}: {title} — Google/SSO sign-in blocks "
                "automated browsers; must apply in your own browser")
            finish("needs_review", "requires Google/SSO sign-in — open in "
                   "your normal browser and apply manually")
            return 0

        # Only hunt for an Apply button when we're NOT already on a direct
        # form, or when the direct form somehow rendered no fields.
        need_click = (not direct) or (
            page.locator("input:not([type=hidden]):visible, "
                         "textarea:visible, select:visible").count() == 0)
        if need_click:
            for text in ["Apply for this job", "Apply Now", "Apply now", "Apply"]:
                for kind in ("button", "link"):
                    loc = page.get_by_role(kind, name=text)
                    if loc.count() and loc.first.is_visible():
                        loc.first.click()
                        page.wait_for_timeout(2500)
                        break
                else:
                    continue
                break

        report = fill_page(page)
        page.wait_for_timeout(250)
        blockers = find_blockers(page)
        # guard against empty submissions: if the form never populated,
        # the apply page likely didn't load — never blind-submit a blank form.
        if not (report["filled"] or report["chosen"] or report["files"]):
            blockers.append("no fields filled — form may not have loaded")
        page.screenshot(path=str(shot), full_page=True)

        for k, v in report.items():
            for line in v:
                log(f"  {k}: {line}")

        counts = (f"{len(report['filled'])} text, {len(report['chosen'])} "
                  f"choices, {len(report['files'])} files")

        # ---------- precheck: grade readiness, never submit or change status ----
        if precheck:
            verdict = classify_readiness(report, blockers)
            con.execute(
                "UPDATE jobs SET readiness=?, readiness_at=? WHERE id=?",
                (verdict, datetime.now(timezone.utc).isoformat(), jid))
            # keep the displayed reason current for jobs already parked in
            # Review — otherwise the first attempt's blockers show forever
            con.execute(
                "UPDATE jobs SET reason=? WHERE id=? AND status='needs_review'",
                ("; ".join(blockers) if blockers else None, jid))
            con.commit()
            con.close()
            log(f"PRECHECK {company}: {title} -> {verdict} ({counts})")
            return 0

        # ---------- dry run: report only, never touch the DB ----------
        if dry_run:
            if blockers:
                log(f"DRY-RUN REVIEW {company}: {title} — " + "; ".join(blockers))
            else:
                log(f"DRY-RUN OK {company}: {title} — would submit ({counts})")
            con.close()
            return 0

        # ---------- headless / batch: no human present ----------
        if not visible:
            if blockers:
                log(f"REVIEW {company}: {title} — " + "; ".join(blockers))
                finish("needs_review", "; ".join(blockers)[:300])
                notify("JobScout: review needed 🔍",
                       f"{company} — {title}: {blockers[0]}")
                return 0
            submit = page.locator(
                "button[type='submit'], input[type='submit'], "
                "button:has-text('Submit application'), "
                "button:has-text('Submit Application'), button:has-text('Submit')")
            if not submit.count():
                finish("needs_review", "no submit button found")
                notify("JobScout: review needed 🔍", f"{company} — {title}")
                return 0
            submit.first.click()
            page.wait_for_timeout(4000)
            if submission_confirmed(page):
                page.screenshot(path=str(SHOTS / f"{jid[:12]}-done.png"), full_page=True)
                log(f"SUBMITTED {company}: {title} ({url})")
                finish("auto_applied")
                notify("JobScout: applied ✅", f"{company} — {title}")
            else:
                page.screenshot(path=str(SHOTS / f"{jid[:12]}-after.png"), full_page=True)
                finish("needs_review", "submit clicked, confirmation not detected")
                notify("JobScout: check application 🔍", f"{company} — {title}")
            return 0

        # ---------- visible: a human is here (assist or review) ----------
        # High-profile / review-mode is fill-only (never auto-click submit).
        # Assist mode auto-clicks submit ONLY on a clean form; if there are
        # blockers (e.g. an interactive CAPTCHA) the user resolves them first.
        review_mode = review or hand_review
        auto_submit = (not review_mode) and (not blockers)
        tag = "reviewed" if review_mode else "assisted"
        # review-mode = YOU submitted it -> 'applied'; assisted = engine clicked
        # Submit (you only cleared a gate) -> 'auto_applied'
        done_status = "applied" if review_mode else "auto_applied"

        if auto_submit:
            submit = page.locator(
                "button[type='submit'], input[type='submit'], "
                "button:has-text('Submit application'), "
                "button:has-text('Submit Application'), button:has-text('Submit')")
            if submit.count():
                submit.first.click()
                page.wait_for_timeout(4000)

        if submission_confirmed(page):
            page.screenshot(path=str(SHOTS / f"{jid[:12]}-done.png"), full_page=True)
            log(f"SUBMITTED ({tag}) {company}: {title} ({url})")
            finish(done_status, tag)
            notify("JobScout: applied ✅", f"{company} — {title}")
            return 0

        # Not done yet — keep the window open and wait for the user to finish.
        if review_mode:
            action = "review the pre-filled form and click Submit"
        elif blockers:
            action = detected_gate(page) if "CAPTCHA" in "".join(blockers) \
                else blockers[0]
        else:
            action = detected_gate(page)  # post-submit gate (e.g. email code)
        page.screenshot(path=str(SHOTS / f"{jid[:12]}-assist.png"), full_page=True)

        # experimental: auto-fetch & enter an email verification code
        if action == "email verification code" and code_inbox_enabled():
            log(f"AUTO-CODE {company}: {title} — watching inbox for the code…")
            since = datetime.now(timezone.utc).timestamp() - 300  # last 5 min only
            for _ in range(int(CONFIG["code_inbox"].get("max_wait_s", 120) / 5)):
                code = fetch_verification_code(since_epoch=since)
                if code and enter_verification_code(page, code):
                    log(f"AUTO-CODE {company}: entered {code}")
                    page.wait_for_timeout(3000)
                    break
                page.wait_for_timeout(5000)

        log(f"AWAIT {company}: {title} — window open; you: {action}")
        notify("JobScout: finish in browser 🙋",
               f"{company} — {title}: {action}")
        timeout_s = REVIEW_TIMEOUT_S if review_mode else ASSIST_TIMEOUT_S
        waited = 0
        while waited < timeout_s:
            if page.is_closed():
                log(f"AWAIT CLOSED {company}: {title} — window closed before submit")
                finish("needs_review", f"filled — awaiting your submit ({action})"[:300])
                return 0
            page.wait_for_timeout(ASSIST_POLL_MS)
            waited += ASSIST_POLL_MS / 1000
            try:
                if submission_confirmed(page):
                    page.screenshot(path=str(SHOTS / f"{jid[:12]}-done.png"),
                                    full_page=True)
                    log(f"SUBMITTED ({tag}) {company}: {title} ({url})")
                    finish(done_status, tag)
                    notify("JobScout: applied ✅", f"{company} — {title}")
                    return 0
            except Exception:
                pass
        log(f"AWAIT TIMEOUT {company}: {title} — not completed in {timeout_s}s")
        finish("needs_review", f"filled — awaiting your submit ({action})"[:300])
        notify("JobScout: not finished ⏳", f"{company} — {title}")
    except Exception as e:
        try:
            page.screenshot(path=str(shot), full_page=True)
        except Exception:
            pass
        log(f"ERROR {company}: {title} — {e}")
        finish("needs_review", f"error: {e}"[:300])
        if not dry_run:
            notify("JobScout: review needed 🔍", f"{company} — {title}")
    finally:
        try:
            context.close()
        except Exception:
            pass
        if own_pw:
            try:
                browser.close()
            except Exception:
                pass
            try:
                pw.stop()
            except Exception:
                pass
    return 0


def run_all(dry_run=False, limit=None, tier=None, shard=None, assist=False,
            ready_only=False, include_review=False):
    """Zero-touch batch: walk every 'new' job (hot first), auto-apply to the
    ones not on the hand-review list. run() itself enforces the hand-review
    gate and weekly cap, so this just picks the queue and reports a tally.
    shard=(k, n) processes only every n-th job starting at k (for parallelism).
    assist=True runs a visible browser and pauses at verification gates.
    ready_only=True limits to jobs graded 'ready' by the pre-check.
    include_review=True (assist only) also queues needs_review jobs —
    hand-review companies included, since run() forces review mode (never
    auto-submits) for them; quick wins (ready, then captcha-only) first."""
    con = sqlite3.connect(DB_PATH, timeout=30)
    if include_review and assist:
        sql = "SELECT id, company, title FROM jobs WHERE status IN ('new','needs_review')"
    else:
        sql = "SELECT id, company, title FROM jobs WHERE status='new'"
    params = []
    if ready_only:
        sql += " AND readiness='ready'"
    if tier:
        sql += " AND tier=?"
        params.append(tier)
    sql += (" ORDER BY (tier='hot') DESC,"
            " CASE readiness WHEN 'ready' THEN 0 WHEN 'captcha' THEN 1"
            " WHEN 'form-issue' THEN 2 ELSE 3 END, first_seen ASC")
    rows = con.execute(sql, params).fetchall()
    con.close()

    if include_review and assist:
        eligible, skipped = rows, 0  # visible run: review mode guards HR cos
    else:
        hr = hand_review_set()
        eligible = [r for r in rows if (r[1] or "").lower() not in hr]
        skipped = len(rows) - len(eligible)
    if limit:
        eligible = eligible[:limit]
    tag = ""
    if shard:
        k, n = shard
        eligible = eligible[k::n]
        tag = f" [shard {k + 1}/{n}]"

    mode = "DRY-RUN" if dry_run else "LIVE"
    log(f"=== BATCH {mode}{tag}: {len(eligible)} eligible "
        f"({skipped} hand-review skipped){' · limit '+str(limit) if limit else ''} ===")

    # Headless batch shares ONE browser (fast, no leaked helpers). But a VISIBLE
    # (assisted) batch must give each job its OWN browser window — sharing one
    # headed browser across windows is fragile (closing/reusing one cascades).
    from playwright.sync_api import sync_playwright
    shared = None
    pw = None
    if not assist:
        pw = sync_playwright().start()
        shared = pw.chromium.launch(headless=True, args=launch_args(False))
    try:
        for jid, company, title in eligible:
            try:
                run(jid, dry_run=dry_run, browser=shared, assist=assist)
            except Exception as e:
                log(f"ERROR {company}: {title} — batch-level: {e}")
    finally:
        if shared:
            try:
                shared.close()
            except Exception:
                pass
            try:
                pw.stop()
            except Exception:
                pass
    log(f"=== BATCH {mode}{tag} done: {len(eligible)} processed ===")
    return 0


def run_all_parallel(workers, dry_run=False, limit=None, tier=None):
    """Fan the batch across `workers` subprocesses (each its own browser), every
    worker taking a disjoint shard of the queue. Roughly an N-times speedup."""
    base = [sys.executable, str(Path(__file__).resolve()), "--all"]
    if dry_run:
        base.append("--dry-run")
    if limit:
        base.append(f"--limit={limit}")
    if tier:
        base.append(f"--tier={tier}")
    log(f"=== PARALLEL {'DRY-RUN' if dry_run else 'LIVE'}: {workers} workers ===")
    procs = [subprocess.Popen(base + [f"--shard={k}:{workers}"])
             for k in range(workers)]
    rc = 0
    for p in procs:
        rc |= p.wait()
    log(f"=== PARALLEL done: {workers} workers ===")
    return rc


def precheck_all(limit=None, shard=None, recheck=False):
    """Headlessly fill every inbox/review job and grade its readiness (ready /
    missing-info / captcha / form-issue) for the dashboard badge. Never submits
    or changes status. recheck=True re-grades jobs already graded."""
    con = sqlite3.connect(DB_PATH, timeout=30)
    sql = "SELECT id, company, title FROM jobs WHERE status IN ('new','needs_review')"
    if not recheck:
        sql += " AND readiness IS NULL"
    sql += " ORDER BY (tier='hot') DESC, first_seen ASC"
    rows = con.execute(sql).fetchall()
    con.close()
    if limit:
        rows = rows[:limit]
    tag = ""
    if shard:
        k, n = shard
        rows = rows[k::n]
        tag = f" [shard {k + 1}/{n}]"
    log(f"=== PRECHECK{tag}: {len(rows)} jobs ===")
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True, args=BROWSER_ARGS)
    try:
        for jid, company, title in rows:
            try:
                run(jid, precheck=True, browser=browser)
            except Exception as e:
                log(f"PRECHECK ERROR {company}: {title} — {e}")
    finally:
        try:
            browser.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass
    log(f"=== PRECHECK{tag} done: {len(rows)} ===")
    return 0


def precheck_all_parallel(workers, limit=None, recheck=False):
    base = [sys.executable, str(Path(__file__).resolve()), "--precheck", "--all"]
    if limit:
        base.append(f"--limit={limit}")
    if recheck:
        base.append("--recheck")
    log(f"=== PRECHECK PARALLEL: {workers} workers ===")
    procs = [subprocess.Popen(base + [f"--shard={k}:{workers}"])
             for k in range(workers)]
    rc = 0
    for p in procs:
        rc |= p.wait()
    log("=== PRECHECK PARALLEL done ===")
    return rc


if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--reconcile" in argv:
        sys.exit(0 if reconcile_from_inbox() >= 0 else 1)
    if "--test-code" in argv:
        c = CONFIG.get("code_inbox", {})
        print(f"inbox user : {c.get('user')}")
        print(f"password   : {'found' if _imap_password() else 'MISSING'}")
        print(f"enabled    : {code_inbox_enabled()}")
        if code_inbox_enabled():
            print("connecting to IMAP and scanning latest messages…")
            print(f"latest code found: {fetch_verification_code()!r}")
        sys.exit(0)
    dry = "--dry-run" in argv
    assist = "--assist" in argv
    review = "--review" in argv  # fill + let the human review & submit
    precheck = "--precheck" in argv  # grade readiness, never submit
    recheck = "--recheck" in argv    # re-grade already-graded jobs
    ready_only = "--ready-only" in argv  # batch only 'ready' jobs
    include_review = "--include-review" in argv  # assist batch incl. Review bin
    positional = [a for a in argv if not a.startswith("--")]
    limit = None
    tier = None
    workers = 1
    shard = None
    for a in argv:
        if a.startswith("--limit="):
            limit = int(a.split("=", 1)[1])
        elif a.startswith("--tier="):
            tier = a.split("=", 1)[1]
        elif a.startswith("--workers="):
            workers = max(1, int(a.split("=", 1)[1]))
        elif a.startswith("--shard="):
            k, n = a.split("=", 1)[1].split(":")
            shard = (int(k), int(n))

    # a visible window needs a human at the keyboard -> one at a time;
    # parallel sharding is incompatible with it.
    if assist or review:
        workers = 1

    if precheck:
        if "--all" in argv:
            if workers > 1 and shard is None:
                sys.exit(precheck_all_parallel(workers, limit=limit, recheck=recheck))
            sys.exit(precheck_all(limit=limit, shard=shard, recheck=recheck))
        if not positional:
            print(__doc__)
            sys.exit(1)
        sys.exit(run(positional[0], precheck=True))

    if "--all" in argv:
        if workers > 1 and shard is None:
            sys.exit(run_all_parallel(workers, dry_run=dry, limit=limit, tier=tier))
        sys.exit(run_all(dry_run=dry, limit=limit, tier=tier, shard=shard,
                         assist=assist, ready_only=ready_only,
                         include_review=include_review))
    if not positional:
        print(__doc__)
        sys.exit(1)
    sys.exit(run(positional[0], dry_run=dry, assist=assist, review=review))
