#!/usr/bin/env python3
"""JobScout dashboard — local web UI at http://localhost:8765

Browse every match JobScout has found, search/filter, open postings, and track
your pipeline (new → applied / hidden). Runs entirely on your machine; binds to
127.0.0.1 only.

Usage:
  python3 dashboard.py            # serve until Ctrl-C
"""

import json
import re
import sqlite3
import subprocess
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "jobs.db"
VENV_PY = BASE / ".venv" / "bin" / "python"
LOGO_DIR = BASE / "logos"
PORT = 8765
BROWSER_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}

try:
    _cfg = json.loads((BASE / "config.json").read_text())
    HAND_REVIEW = {c.lower() for c in
                   _cfg.get("auto_apply", {}).get("hand_review_companies", [])}
except Exception:
    HAND_REVIEW = set()

# job_id -> Popen of an in-flight assisted-submit, so one job can't open two windows
RUNNING = {}
# name -> Popen of an in-flight batch job (precheck / apply_all), single-flighted
BATCH = {}

PAGE = """<!doctype html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>JobScout</title>
<style>
:root { --bg:#eef0f4; --fg:#1a1a2e; --muted:#667; --card:#fff; --elev:#f6f7f9;
        --line:#dcdfe4; --accent:#3056d3; --hot:#c2410c; --gold:#b8860b;
        --ok:#15803d; --warn:#b45309;
        --shadow:0 1px 2px rgba(20,22,40,.07), 0 3px 10px rgba(20,22,40,.05);
        --shadow-lg:0 2px 6px rgba(20,22,40,.10), 0 8px 24px rgba(20,22,40,.08); }
@media (prefers-color-scheme: dark) {
  :root { --bg:#0e1013; --fg:#e8eaed; --muted:#9aa0a6; --card:#1c1f26;
          --elev:#262a33; --line:#333944; --accent:#7c9aff; --hot:#fdba74;
          --gold:#f5b301; --ok:#86efac; --warn:#fcd34d;
          --shadow:0 1px 2px rgba(0,0,0,.5), 0 3px 12px rgba(0,0,0,.35);
          --shadow-lg:0 2px 6px rgba(0,0,0,.55), 0 10px 30px rgba(0,0,0,.45); }
}
* { box-sizing:border-box; margin:0; }
body { background:var(--bg); color:var(--fg);
       font:15px/1.5 -apple-system, "SF Pro Text", Helvetica, sans-serif; }
header { display:flex; flex-wrap:wrap; gap:.75rem; align-items:center;
         padding:1rem 1.25rem; border-bottom:1px solid var(--line);
         position:sticky; top:0; background:var(--card); z-index:2;
         box-shadow:var(--shadow); }
h1 { font-size:1.15rem; margin-right:.5rem; }
h1 span { color:var(--muted); font-weight:400; font-size:.85rem; }
input[type=search] { flex:1; min-width:180px; padding:.45rem .7rem;
  border:1px solid var(--line); border-radius:8px; background:var(--card);
  color:var(--fg); font-size:.95rem; }
.tabs { display:flex; gap:.25rem; }
.tabs button { border:1px solid var(--line); background:var(--card);
  color:var(--muted); padding:.35rem .8rem; border-radius:8px; cursor:pointer;
  font-size:.85rem; }
.tabs button.active { color:var(--fg); border-color:var(--accent);
  background:transparent; font-weight:600; }
main { max-width:900px; margin:0 auto; padding:1rem 1.25rem 4rem; }
.day { color:var(--muted); font-size:.78rem; font-weight:700;
       text-transform:uppercase; letter-spacing:.06em; margin:1.1rem 0 .5rem; }
.job { display:flex; gap:1rem; align-items:center; padding:.85rem 1rem;
       border:1px solid var(--line); border-left:3px solid var(--line);
       border-radius:10px; margin-bottom:.7rem; background:var(--card);
       box-shadow:var(--shadow); transition:transform .12s ease,
       box-shadow .12s ease, background .12s ease; }
.job:hover { background:var(--elev); box-shadow:var(--shadow-lg);
       transform:translateY(-1px); }
.job.hot { border-left-color:var(--hot); }
.job.hr { border-left-color:var(--gold); }
.job .body { flex:1; min-width:0; }
.logo { flex:none; width:72px; height:72px; border-radius:14px; background:var(--line);
        display:flex; align-items:center; justify-content:center; position:relative;
        color:var(--muted); font-weight:700; font-size:1.6rem; }
.logo img { position:absolute; inset:0; width:100%; height:100%;
            border-radius:14px; object-fit:contain; padding:8px; background:#fff; }
.job .top { display:flex; gap:.6rem; align-items:baseline; flex-wrap:wrap; }
.job a.title { color:var(--accent); font-weight:600; text-decoration:none;
               font-size:1rem; }
.job a.title:hover { text-decoration:underline; }
.chip { font-size:.78rem; font-weight:600; padding:.05rem .55rem;
        border-radius:99px; border:1px solid; white-space:nowrap;
        max-width:100%; overflow:hidden; text-overflow:ellipsis; }
.chip.ok { color:var(--ok); border-color:var(--ok); }
.chip.warn { color:var(--warn); border-color:var(--warn); }
.meta { color:var(--muted); font-size:.83rem; margin-top:.15rem; }
.actions { flex:none; display:flex; gap:.5rem; align-items:center; }
.actions button { border:1px solid var(--line); background:transparent;
  color:var(--muted); border-radius:7px; padding:.25rem .7rem; cursor:pointer;
  font-size:.8rem; }
.actions button:hover { color:var(--fg); border-color:var(--muted); }
.actions button.apply { color:#fff; background:var(--accent); border-color:var(--accent);
  font-weight:700; }
.actions button.apply:hover { filter:brightness(1.1); }
.actions button.apply:disabled { opacity:.7; cursor:default; }
.actions button.review { color:#fff; background:#8b5cf6; border-color:#8b5cf6;
  font-weight:700; }
.actions button.review:hover { filter:brightness(1.1); }
.actions button.review:disabled { opacity:.7; cursor:default; }
.actions button.warn { color:var(--warn); border-color:var(--warn);
  background:transparent; font-weight:600; }
.actions button.warn:hover { color:var(--warn); filter:brightness(1.1); }
details.more { position:relative; }
details.more summary { list-style:none; cursor:pointer; border:1px solid var(--line);
  border-radius:7px; padding:.25rem .6rem; color:var(--muted); font-size:.8rem;
  user-select:none; }
details.more summary::-webkit-details-marker { display:none; }
details.more[open] summary { color:var(--fg); border-color:var(--muted); }
details.more .menu { position:absolute; top:110%; right:0; z-index:3;
  background:var(--bg); border:1px solid var(--line); border-radius:8px;
  padding:.25rem; display:flex; flex-direction:column; min-width:150px;
  box-shadow:0 4px 16px rgba(0,0,0,.18); }
details.more .menu button { border:none; background:none; color:var(--fg);
  text-align:left; padding:.35rem .6rem; border-radius:6px; cursor:pointer;
  font-size:.82rem; }
details.more .menu button:hover { background:var(--card); }
.toolbar { display:flex; gap:.5rem; flex-wrap:wrap; align-items:center; }
.toolbar button { border:1px solid var(--line); background:var(--card);
  color:var(--muted); border-radius:8px; padding:.35rem .8rem; cursor:pointer;
  font-size:.85rem; }
.toolbar button.apply { color:#fff; background:var(--accent); border-color:var(--accent);
  font-weight:700; }
.toolbar button:disabled { opacity:.7; cursor:default; }
.fit { display:inline-block; font-size:.72rem; font-weight:700;
  color:var(--muted); background:rgba(148,148,148,.14);
  border:1px solid rgba(148,148,148,.35); border-radius:6px;
  padding:.05rem .4rem; margin-right:.4rem; white-space:nowrap; }
.fit.top { color:#7c5cff; background:rgba(124,92,255,.14);
  border-color:rgba(124,92,255,.5); }
.empty { color:var(--muted); text-align:center; padding:3rem 0; }
footer { color:var(--muted); font-size:.78rem; text-align:center;
         padding:1rem; }
</style></head>
<body>
<header>
  <h1>JobScout <span id="count"></span></h1>
  <input id="q" type="search" placeholder="Search title, company, location…">
  <div class="tabs" id="tabs">
    <button data-s="new" class="active">Inbox</button>
    <button data-s="needs_review">Review</button>
    <button data-s="submitted">✅ Submitted</button>
    <button data-s="hidden">Hidden</button>
    <button data-s="all">All</button>
  </div>
  <div class="toolbar">
    <button class="apply" onclick="applyAllReady(this)" title="assisted-apply every ready inbox job, one window at a time">🚀 Apply all ready</button>
    <button onclick="assistAll(this)" title="walk EVERY inbox + review job, one visible window at a time — quick wins first; you clear gates and click submit">🙋 Assist all</button>
    <button onclick="recheck(this)" title="re-grade readiness of all inbox/review jobs">🔄 Re-check readiness</button>
    <button onclick="rankFits(this)" title="LLM-rank the best-fit jobs for you — type a company in search first to scope it (e.g. amazon)">🎯 Rank fits</button>
  </div>
</header>
<main id="list"></main>
<footer>Local-only dashboard · poller runs in the background</footer>
<script>
let status = "new", q = "", timer = null;
const list = document.getElementById("list");
const TABS = { new:"Inbox", needs_review:"Review", submitted:"✅ Submitted",
               hidden:"Hidden", all:"All" };
const BUCKETS = ["Today", "Yesterday", "This week", "Older"];

async function load(force = true) {
  const r = await fetch(`/api/jobs?status=${status}&q=${encodeURIComponent(q)}`);
  const { jobs, counts } = await r.json();
  renderTabs(counts);
  document.getElementById("count").textContent = `· ${jobs.length} shown`;
  // don't yank an open ⋯ menu shut on the background refresh
  if (!force && list.querySelector("details[open]")) return;
  if (!jobs.length) { list.innerHTML = '<div class="empty">Nothing here.</div>'; return; }
  const groups = new Map(BUCKETS.map(k => [k, []]));
  jobs.forEach(j => groups.get(bucket(j.first_seen)).push(j));
  list.innerHTML = BUCKETS.filter(k => groups.get(k).length).map(k =>
    `<div class="day">${k}</div>` + groups.get(k).map(card).join("")).join("");
}
const esc = s => s.replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function renderTabs(counts) {
  document.querySelectorAll("#tabs button").forEach(b => {
    const n = counts[b.dataset.s];
    b.textContent = TABS[b.dataset.s] + (n ? ` · ${n}` : "");
  });
}
function bucket(iso) {
  const d = new Date(iso), now = new Date();
  const day = x => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((day(now) - day(d)) / 86400000);
  if (diff <= 0) return "Today";
  if (diff === 1) return "Yesterday";
  if (diff < 7) return "This week";
  return "Older";
}
function card(j) {
  const cls = "job" + (j.hand_review ? " hr" : j.tier === "hot" ? " hot" : "");
  const tierNote = j.hand_review ? " · high-profile" : j.tier === "hot" ? " · early career" : "";
  return `
    <div class="${cls}">
      ${logo(j)}
      <div class="body">
        <div class="top">
          ${j.fit_display ? `<span class="fit top" title="${esc(j.fit_note||'')}">🎯 #${j.fit_display} fit</span> ` : ""}
          <a class="title" href="${j.url}" target="_blank">${esc(j.title)}</a>
          ${chip(j)}
        </div>
        <div class="meta">${esc(j.company)} · ${esc(j.location || "n/a")} ·
          <span title="${new Date(j.first_seen).toLocaleString()}">first seen ${seenAgo(j.first_seen)}</span> · via ${j.source}${tierNote}</div>
      </div>
      <div class="actions">
        ${(j.status === "new" || j.status === "needs_review") ? applyBtn(j) : ""}
        ${more(j)}
      </div>
    </div>`;
}
// board slugs whose website isn't just "<slug>.com"
const DOMAIN_ALIAS = { doordashusa:"doordash.com", andurilindustries:"anduril.com",
  scaleai:"scale.com", togetherai:"together.ai", xai:"x.ai",
  harvey:"harvey.ai", decagon:"decagon.ai" };
function logo(j) {
  const name = (j.company || "?").trim() || "?";
  const slug = name.toLowerCase().replace(/[^a-z0-9]/g, "");
  const dom = DOMAIN_ALIAS[slug] || slug + ".com";
  return `<div class="logo">${esc(name[0].toUpperCase())}<img
    src="/logo/${dom}" loading="lazy" alt=""
    onerror="this.remove()"></div>`;
}
function chip(j) {
  if (j.status === "applied" || j.status === "auto_applied") {
    const t = j.reason || "";
    const label = t.includes("reviewed") ? "⭐ you reviewed &amp; sent"
      : t.includes("assisted") ? "🙋 you cleared a gate"
      : t.includes("confirmed by email") ? "📬 confirmed by email"
      : j.status === "auto_applied" ? "🤖 fully auto" : "✓ marked applied";
    return `<span class="chip ok">${label}</span>`;
  }
  // Don't repeat what the apply button already says: readiness-driven buttons
  // (needs-info / captcha / form-issue / open-in-Chrome) and the gold-border
  // high-profile button carry the state — a chip would just restate it.
  const buttonCovers = ["missing-info", "captcha", "form-issue", "manual"]
    .includes(j.readiness);
  const hpCovers = j.hand_review && /high.?profile/i.test(j.reason || "");
  if (j.status === "needs_review" && j.reason && !buttonCovers && !hpCovers)
    return `<span class="chip warn" title="${esc(j.reason)}">⚠ ${esc(shortReason(j.reason))}</span>`;
  return "";
}
// compress raw precheck output into a short scannable label; full text is in
// the tooltip. Keep chips to ~2-3 words so cards stay uncluttered.
function shortReason(t) {
  const map = [
    [/bot.?block|unusual activity|temporarily restricted/i, "bot-blocked — retry"],
    [/sso|supported browser|sign.?in/i, "needs SSO sign-in"],
    [/opened in your chrome/i, "opened in Chrome"],
    [/weekly.*cap/i, "weekly cap hit"],
    [/removed or closed|no longer/i, "posting closed"],
  ];
  for (const [re, label] of map) if (re.test(t)) return label;
  const parts = [];
  if (/captcha/i.test(t)) parts.push("captcha");
  const miss = (t.match(/required field empty/gi) || []).length;
  if (miss) parts.push(`${miss} missing field${miss > 1 ? "s" : ""}`);
  if (/\\berror\\b/i.test(t)) parts.push("error");
  if (parts.length) return parts.join(" · ");
  return t.length > 40 ? t.slice(0, 37) + "…" : t;
}
function applyBtn(j) {
  // manual-apply (SSO/automation-blocked) wins even for high-profile companies —
  // those can't be driven in the controlled browser at all
  if (j.readiness === "manual")
    return `<button class="warn" onclick="applyNow('${j.id}', this)" title="SSO/automation-blocked — opens in your Chrome; press ⌘⇧J to autofill">🧑‍💻 Open in Chrome</button>`;
  if (j.hand_review)
    return `<button class="review" onclick="applyNow('${j.id}', this)" title="fills the form, then you review & submit">🔍 Review &amp; submit</button>`;
  const state = {
    "missing-info": ["⚠ Apply — needs info", "fills what it can; you complete the rest"],
    captcha:        ["🔒 Apply — captcha",   "fills the form; you clear the captcha"],
    "form-issue":   ["⛔ Apply — form issue","opens the form so you can finish by hand"],
  }[j.readiness];
  if (state)
    return `<button class="warn" onclick="applyNow('${j.id}', this)" title="${state[1]}">${state[0]}</button>`;
  return `<button class="apply" onclick="applyNow('${j.id}', this)">🚀 Apply</button>`;
}
function more(j) {
  const items =
    (j.status !== "applied" ? mi(j.id, "applied", "Mark applied") : "") +
    (j.status !== "hidden"  ? mi(j.id, "hidden", "Hide") : "") +
    (j.status !== "new"     ? mi(j.id, "new", "Move to inbox") : "");
  return `<details class="more"><summary>⋯</summary><div class="menu">${items}</div></details>`;
}
const mi = (id, s, label) => `<button onclick="setStatus('${id}','${s}')">${label}</button>`;
function seenAgo(iso) {                    // relative time in the viewer's local zone
  const then = new Date(iso), now = new Date(), s = (now - then) / 1000;
  if (s < 90) return "just now";
  if (s < 3600) return Math.round(s/60) + "m ago";
  if (s < 86400) return Math.round(s/3600) + "h ago";
  const d = Math.round(s/86400);
  return d === 1 ? "yesterday" : d + "d ago";
}
async function setStatus(id, s) {
  await fetch("/api/status", {method:"POST",
    headers:{"Content-Type":"application/json"},
    body: JSON.stringify({id, status:s})});
  load();
}
async function applyNow(id, el) {
  el.disabled = true; el.textContent = "⏳ opening…";
  try {
    const r = await fetch("/api/apply", {method:"POST",
      headers:{"Content-Type":"application/json"}, body: JSON.stringify({id})});
    const j = await r.json();
    if (j.status === "already running") {
      el.textContent = "already open";
    } else if (j.mode === "review") {
      el.textContent = "✅ window open — review & submit";
    } else {
      el.textContent = "✅ window open — finish in browser";
    }
  } catch (e) { el.textContent = "⚠ failed"; el.disabled = false; }
}
async function rankFits(el) {
  const company = (document.getElementById("q").value || "").trim();
  el.disabled = true; el.textContent = "🎯 ranking…";
  try {
    const r = await fetch("/api/rank", {method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({company})});
    const d = await r.json();
    el.textContent = d.status === "already running"
      ? "🎯 already running" : "🎯 ranking… (refresh soon)";
  } catch { el.textContent = "🎯 failed"; }
  setTimeout(() => { el.disabled = false; el.textContent = "🎯 Rank fits";
    load(); }, 8000);
}
async function assistAll(el) {
  el.disabled = true; el.textContent = "🙋 launching…";
  try {
    const r = await fetch("/api/assist_all", {method:"POST"});
    const d = await r.json();
    el.textContent = d.status === "already running"
      ? "🙋 already running" : `🙋 assisting ${d.count} jobs…`;
  } catch { el.textContent = "🙋 failed"; }
  setTimeout(() => { el.disabled = false; el.textContent = "🙋 Assist all"; }, 5000);
}
async function applyAllReady(el) {
  el.disabled = true; const t = el.textContent; el.textContent = "⏳ starting…";
  try {
    const r = await fetch("/api/apply_all", {method:"POST"});
    const j = await r.json();
    el.textContent = `🚀 applying ${j.count} ready…`;
  } catch(e) { el.textContent = "⚠ failed"; el.disabled = false; return; }
  setTimeout(() => { el.textContent = t; el.disabled = false; }, 8000);
}
async function recheck(el) {
  el.disabled = true; const t = el.textContent; el.textContent = "🔄 checking…";
  try { await fetch("/api/precheck", {method:"POST"}); } catch(e) {}
  setTimeout(() => { el.textContent = t; el.disabled = false; }, 8000);
}
document.getElementById("q").addEventListener("input", e => {
  q = e.target.value;
  clearTimeout(timer); timer = setTimeout(load, 200);
});
document.getElementById("tabs").addEventListener("click", e => {
  if (e.target.tagName !== "BUTTON") return;
  status = e.target.dataset.s;
  document.querySelectorAll(".tabs button").forEach(b =>
    b.classList.toggle("active", b === e.target));
  load();
});
document.addEventListener("click", e => {   // click-away closes any open ⋯ menu
  document.querySelectorAll("details.more[open]").forEach(d => {
    if (!d.contains(e.target)) d.removeAttribute("open");
  });
});
load();
// refresh often so assisted submits jump to Submitted on their own
setInterval(() => load(false), 5000);
</script>
</body></html>"""


# ------------------------------------------------------------------- logos
# Favicon services often only have a 16-32px icon, which looks blurry blown
# up to the 72px tile. Resolve each company's own homepage-declared icon
# (apple-touch-icon / sized favicon — usually 180px+) first, fall back to
# Google's favicon service, and refuse anything under 64px so the crisp
# letter tile shows instead of a blurry upscale. Cached on disk forever.

ICON_LINK_RE = re.compile(r"""<link[^>]+rel=["'][^"']*icon[^"']*["'][^>]*>""",
                          re.I)


# fetch via curl: several sites (chime, doordash) block Python's TLS
# fingerprint with a 403 but accept curl's
def _fetch(url, timeout=8):
    r = subprocess.run(
        ["curl", "-sL", "--max-time", str(timeout),
         "-A", BROWSER_UA["User-Agent"],
         "-H", "Accept-Language: en-US,en;q=0.9",
         "-w", "\n__EFFECTIVE_URL__%{url_effective}", url],
        capture_output=True, timeout=timeout + 5)
    body, _, eff = r.stdout.rpartition(b"\n__EFFECTIVE_URL__")
    return body, (eff.decode("ascii", "ignore") or url)


def best_icon_url(page_html, page_url):
    best, best_size = None, 0
    for tag in ICON_LINK_RE.findall(page_html):
        href = re.search(r"""href=["']([^"']+)["']""", tag, re.I)
        if not href:
            continue
        m = re.search(r"""sizes=["'](\d+)""", tag, re.I)
        size = int(m.group(1)) if m else (180 if "apple-touch" in tag else 32)
        if href.group(1).split("?")[0].endswith(".svg"):
            size = max(size, 256)  # scalable
        if size > best_size:
            best = urllib.parse.urljoin(page_url, href.group(1))
            best_size = size
    return best if best_size >= 64 else None


def img_big_enough(data):
    """True only for real image bytes that won't upscale blurry (<64px).
    Anything unrecognized (HTML bot-check pages, error bodies) is rejected."""
    if len(data) < 100:
        return False
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) > 24:
        return int.from_bytes(data[16:20], "big") >= 64
    if data[:4] == b"\x00\x00\x01\x00":  # ICO: check every embedded size
        n = int.from_bytes(data[4:6], "little")
        widths = [(data[6 + 16 * i] or 256) for i in range(n)
                  if len(data) > 6 + 16 * i]
        return bool(widths) and max(widths) >= 64
    if data[:3] == b"\xff\xd8\xff" or data[:4] == b"RIFF" \
            or data[:4] == b"GIF8":
        return True  # jpeg/webp/gif: can't cheaply size; assume fine
    if b"<svg" in data[:300].lower():
        return True  # scalable
    return False


def img_ctype(data):
    if data[:4] == b"\x89PNG":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"\x00\x00\x01\x00":
        return "image/x-icon"
    if data[:4] == b"RIFF":
        return "image/webp"
    if b"<svg" in data[:300].lower():
        return "image/svg+xml"
    return "image/png"


def _itunes_icon(domain):
    """Official 512px app icon via the iTunes Search API — last resort for
    consumer brands whose sites block scraping or only serve tiny favicons.
    The brand must appear in the app/seller name so we never show a wrong
    company's logo."""
    brand = domain.split(".")[0]
    data, _ = _fetch("https://itunes.apple.com/search?term="
                     f"{urllib.parse.quote(brand)}&entity=software"
                     "&limit=1&country=US")
    results = json.loads(data).get("results", [])
    if not results:
        return b""
    # only the top-ranked result: official apps rank first, fan apps don't
    r = results[0]
    hay = (r.get("trackName", "") + " " + r.get("sellerName", "")).lower()
    if re.search(rf"\b{re.escape(brand)}\b", hay) and r.get("artworkUrl512"):
        icon, _ = _fetch(r["artworkUrl512"])
        return icon
    return b""


def resolve_logo(domain):
    """Cached logo bytes for a domain (b'' = nothing crisp; use letter tile)."""
    LOGO_DIR.mkdir(exist_ok=True)
    cache = LOGO_DIR / domain
    if cache.exists():
        return cache.read_bytes()
    data = b""
    hosts = [domain] if domain.startswith("www.") else [domain, "www." + domain]
    for host in hosts:  # some apexes 403 while www serves fine
        try:
            page, final_url = _fetch(f"https://{host}/")
            url = best_icon_url(page.decode("utf-8", "replace"), final_url)
            if url:
                data, _ = _fetch(url)
                if img_big_enough(data):
                    break
        except Exception:
            data = b""
    if not img_big_enough(data):
        try:
            data, _ = _fetch("https://www.google.com/s2/favicons?sz=128"
                             f"&domain={domain}")
        except Exception:
            data = b""
    if not img_big_enough(data):
        try:
            data = _itunes_icon(domain)
        except Exception:
            data = b""
    if not img_big_enough(data):
        data = b""  # negative-cache: letter tile beats a blurry upscale
    tmp = cache.with_name(domain + ".tmp")
    tmp.write_bytes(data)
    tmp.rename(cache)
    return data


def query_jobs(status, q):
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    for col in ("fit_rank INTEGER", "fit_note TEXT"):  # may predate rank feature
        try:
            con.execute(f"ALTER TABLE jobs ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    sql = ("SELECT id, source, company, title, location, url, tier, first_seen, "
           "status, reason, readiness, fit_rank, fit_note FROM jobs WHERE 1=1")
    params = []
    if status == "submitted":
        sql += " AND status IN ('applied', 'auto_applied')"
    elif status and status != "all":
        sql += " AND status = ?"
        params.append(status)
    if q:
        sql += " AND (title LIKE ? OR company LIKE ? OR location LIKE ?)"
        params += [f"%{q}%"] * 3
    sql += " ORDER BY CASE tier WHEN 'hot' THEN 0 ELSE 1 END, first_seen DESC LIMIT 500"
    rows = [dict(r) for r in con.execute(sql, params)]
    counts = dict(con.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status"))
    con.close()
    for r in rows:
        r["hand_review"] = (r.get("company") or "").lower() in HAND_REVIEW
    # badge the top 3 PER COMPANY among currently-visible jobs. fit_rank holds
    # the LLM's absolute ordering; fit_display is recomputed here each request,
    # so manually hiding a ranked job instantly promotes the next one — no
    # re-rank / LLM call needed. (A full 🎯 re-rank re-scores from scratch.)
    from collections import defaultdict
    by_co = defaultdict(list)
    for r in rows:
        if r.get("fit_rank"):
            by_co[(r.get("company") or "").lower()].append(r)
    for lst in by_co.values():
        lst.sort(key=lambda r: r["fit_rank"])
        for i, r in enumerate(lst, 1):
            r["fit_display"] = i if i <= 3 else None
    if any(r.get("fit_rank") for r in rows):
        rows.sort(key=lambda r: (r.get("fit_rank") is None, r.get("fit_rank") or 0))
    else:
        rows.sort(key=lambda r: not r["hand_review"])  # high-profile first (stable)
    tab_counts = {
        "new": counts.get("new", 0),
        "needs_review": counts.get("needs_review", 0),
        "submitted": counts.get("applied", 0) + counts.get("auto_applied", 0),
        "hidden": counts.get("hidden", 0),
        "all": sum(counts.values()),
    }
    return rows, tab_counts


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            self.send(200, PAGE, "text/html; charset=utf-8")
        elif parsed.path == "/api/jobs":
            qs = urllib.parse.parse_qs(parsed.query)
            jobs, counts = query_jobs(qs.get("status", ["new"])[0],
                                      qs.get("q", [""])[0])
            self.send(200, json.dumps({"jobs": jobs, "counts": counts}))
        elif parsed.path == "/api/fill-spec":
            # the SAME fill rules the Playwright engine uses, served to the
            # Chrome extension so manual applies (SSO/logged-in Chrome) get
            # auto-filled too. one source of truth: auto_apply.py.
            try:
                import auto_apply as aa
                spec = {
                    "text": [[frags, str(val)] for frags, val in aa.TEXT_FIELDS],
                    "choice": aa.CHOICE_RULES,
                    "select": aa.SELECT_ANSWERS,
                }
                body = json.dumps(spec)
            except Exception as e:
                body = json.dumps({"error": str(e)})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body.encode())))
            self.end_headers()
            self.wfile.write(body.encode())
        elif parsed.path.startswith("/logo/"):
            domain = urllib.parse.unquote(parsed.path[6:]).lower()
            if not re.fullmatch(r"[a-z0-9.-]{1,80}", domain):
                self.send(404, "{}")
                return
            data = resolve_logo(domain)
            if data:
                self.send_response(200)
                self.send_header("Content-Type", img_ctype(data))
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "public, max-age=604800")
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send(404, "{}")
        else:
            self.send(404, "{}")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        if self.path == "/api/status":
            if body.get("status") not in ("new", "applied", "hidden",
                                          "needs_review", "auto_applied"):
                self.send(400, "{}")
                return
            con = sqlite3.connect(DB_PATH, timeout=30)
            con.execute("UPDATE jobs SET status=? WHERE id=?",
                        (body["status"], body["id"]))
            con.commit()
            con.close()
            self.send(200, "{}")
            return

        if self.path == "/api/apply":
            jid = body.get("id")
            if not jid:
                self.send(400, json.dumps({"error": "no id"}))
                return
            con = sqlite3.connect(DB_PATH, timeout=30)
            row = con.execute("SELECT company FROM jobs WHERE id=?",
                              (jid,)).fetchone()
            con.close()
            # high-profile -> review mode (fill + you submit); others -> assist
            is_hr = bool(row) and (row[0] or "").lower() in HAND_REVIEW
            mode = "--review" if is_hr else "--assist"
            # don't open a second window for a job already being applied to
            existing = RUNNING.get(jid)
            if existing and existing.poll() is None:
                self.send(200, json.dumps({"status": "already running"}))
                return
            logf = open(BASE / "logs" / "assist.log", "a")
            RUNNING[jid] = subprocess.Popen(
                [str(VENV_PY), str(BASE / "auto_apply.py"), jid, mode],
                cwd=str(BASE), stdout=logf, stderr=logf)
            self.send(200, json.dumps({"status": "launching",
                                       "mode": "review" if is_hr else "assist"}))
            return

        if self.path == "/api/precheck":
            b = BATCH.get("precheck")
            if b and b.poll() is None:
                self.send(200, json.dumps({"status": "already running"}))
                return
            logf = open(BASE / "logs" / "precheck.log", "a")
            BATCH["precheck"] = subprocess.Popen(
                [str(VENV_PY), str(BASE / "auto_apply.py"), "--precheck", "--all",
                 "--recheck", "--workers=4"], cwd=str(BASE), stdout=logf, stderr=logf)
            self.send(200, json.dumps({"status": "launching"}))
            return

        if self.path == "/api/apply_all":
            b = BATCH.get("apply_all")
            if b and b.poll() is None:
                self.send(200, json.dumps({"status": "already running", "count": 0}))
                return
            con = sqlite3.connect(DB_PATH, timeout=30)
            count = con.execute("SELECT COUNT(*) FROM jobs WHERE status='new' "
                                "AND readiness='ready'").fetchone()[0]
            con.close()
            logf = open(BASE / "logs" / "assist.log", "a")
            BATCH["apply_all"] = subprocess.Popen(
                [str(VENV_PY), str(BASE / "auto_apply.py"), "--all", "--assist",
                 "--ready-only"], cwd=str(BASE), stdout=logf, stderr=logf)
            self.send(200, json.dumps({"status": "launching", "count": count}))
            return

        if self.path == "/api/rank":
            b = BATCH.get("rank")
            if b and b.poll() is None:
                self.send(200, json.dumps({"status": "already running"}))
                return
            company = (body.get("company") or "").strip()
            cmd = [str(VENV_PY), str(BASE / "scout.py"), "rank", "--top=3"]
            if company:
                cmd.append(f"--company={company}")
            logf = open(BASE / "logs" / "rank.log", "a")
            BATCH["rank"] = subprocess.Popen(cmd, cwd=str(BASE),
                                             stdout=logf, stderr=logf)
            self.send(200, json.dumps({"status": "launching"}))
            return

        if self.path == "/api/assist_all":
            b = BATCH.get("assist_all")
            if b and b.poll() is None:
                self.send(200, json.dumps({"status": "already running", "count": 0}))
                return
            con = sqlite3.connect(DB_PATH, timeout=30)
            count = con.execute("SELECT COUNT(*) FROM jobs "
                                "WHERE status IN ('new','needs_review')").fetchone()[0]
            con.close()
            logf = open(BASE / "logs" / "assist.log", "a")
            BATCH["assist_all"] = subprocess.Popen(
                [str(VENV_PY), str(BASE / "auto_apply.py"), "--all", "--assist",
                 "--include-review"], cwd=str(BASE), stdout=logf, stderr=logf)
            self.send(200, json.dumps({"status": "launching", "count": count}))
            return

        self.send(404, "{}")


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"JobScout dashboard: http://localhost:{PORT}")
    server.serve_forever()
