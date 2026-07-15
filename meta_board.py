#!/usr/bin/env python3
"""Fetch Meta job postings for scout.py.

metacareers.com renders nothing without JavaScript and its GraphQL API needs
browser-session tokens, so this loads the search page headlessly (Playwright)
and captures the job_search GraphQL responses, scrolling to pull extra pages.

Usage: .venv/bin/python meta_board.py <query> [<query> ...]
Prints a JSON list of {source, company, title, location, url} to stdout.
"""
import json
import sys
import urllib.parse

from playwright.sync_api import sync_playwright

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
SCROLLS = 3  # each scroll can trigger another 20-job GraphQL page


def collect(page, query):
    batches = []

    def on_response(resp):
        if "graphql" not in resp.url:
            return
        try:
            body = resp.json()
        except Exception:
            return
        jobs = ((body.get("data") or {})
                .get("job_search_with_featured_jobs_v2") or {}).get("all_jobs")
        if jobs:
            batches.append(jobs)

    page.on("response", on_response)
    page.goto("https://www.metacareers.com/jobsearch/?q="
              + urllib.parse.quote_plus(query),
              wait_until="networkidle", timeout=60000)
    for _ in range(SCROLLS):
        page.mouse.wheel(0, 20000)
        page.wait_for_timeout(1500)
    page.remove_listener("response", on_response)
    return [j for batch in batches for j in batch]


def main():
    queries = sys.argv[1:]
    if not queries:
        print("usage: meta_board.py <query> [<query> ...]", file=sys.stderr)
        sys.exit(2)
    seen, out = set(), []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(user_agent=UA)
        for q in queries:
            for j in collect(page, q):
                jid = j.get("id")
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                out.append({
                    "source": "meta",
                    "company": "meta",
                    "title": j.get("title", ""),
                    "location": "; ".join(j.get("locations") or []),
                    "url": f"https://www.metacareers.com/jobs/{jid}",
                })
        browser.close()
    json.dump(out, sys.stdout)


if __name__ == "__main__":
    main()
