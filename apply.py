#!/usr/bin/env python3
"""JobScout apply helper — review-then-apply.

Opens the job's application page in a visible Chromium window, fills in your
contact details from profile.json, and attaches your resume. It NEVER submits:
you review the form, answer anything it couldn't fill (visa questions, custom
essays), and click Submit yourself.

Usage:
  python3 apply.py <job_url>

One-time setup:
  python3 -m pip install playwright
  python3 -m playwright install chromium
"""

import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
PROFILE = json.loads((BASE / "profile.json").read_text())

# label/name/id fragments -> profile values, tried in order per field
FIELD_MAP = [
    (["first name", "first_name", "firstname"], PROFILE["first_name"]),
    (["last name", "last_name", "lastname"], PROFILE["last_name"]),
    (["full name", "your name", "name"], PROFILE["full_name"]),
    (["email"], PROFILE["email"]),
    (["phone"], PROFILE["phone"]),
    (["linkedin"], PROFILE["linkedin"]),
    (["github"], PROFILE["github"]),
    (["website", "portfolio"], PROFILE.get("website", "")),
    (["location", "city", "current location"], PROFILE["location"]),
    (["school", "university"], PROFILE.get("school", "")),
]


def fill_form(page):
    filled = []
    inputs = page.locator(
        "input[type='text'], input[type='email'], input[type='tel'], input:not([type])")
    for i in range(inputs.count()):
        el = inputs.nth(i)
        if not el.is_visible():
            continue
        try:
            if el.input_value():
                continue  # don't overwrite anything pre-filled
        except Exception:
            continue
        # gather every hint we can about what this field is
        hints = " ".join(filter(None, [
            (el.get_attribute("name") or ""),
            (el.get_attribute("id") or ""),
            (el.get_attribute("placeholder") or ""),
            (el.get_attribute("aria-label") or ""),
            (el.get_attribute("autocomplete") or ""),
        ])).lower()
        try:
            label = el.evaluate(
                "e => e.labels && e.labels.length ? e.labels[0].textContent : ''") or ""
        except Exception:
            label = ""
        hints += " " + label.lower()
        for fragments, value in FIELD_MAP:
            if value and any(f in hints for f in fragments):
                try:
                    el.fill(value)
                    filled.append(f"{fragments[0]} = {value}")
                except Exception:
                    pass
                break
    return filled


def attach_resume(page):
    resume = PROFILE["resume_path"]
    if not Path(resume).exists():
        print(f"!! resume not found at {resume} — attach manually")
        return False
    file_inputs = page.locator("input[type='file']")
    for i in range(file_inputs.count()):
        try:
            file_inputs.nth(i).set_input_files(resume)
            return True
        except Exception:
            continue
    return False


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    url = sys.argv[1]
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright is not installed. One-time setup:\n"
              "  python3 -m pip install playwright\n"
              "  python3 -m playwright install chromium")
        sys.exit(1)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        # many boards show the description first with an Apply button
        for text in ["Apply for this job", "Apply Now", "Apply now", "Apply"]:
            btn = page.get_by_role("button", name=text)
            link = page.get_by_role("link", name=text)
            try:
                if btn.count() and btn.first.is_visible():
                    btn.first.click()
                    break
                if link.count() and link.first.is_visible():
                    link.first.click()
                    break
            except Exception:
                continue
        page.wait_for_timeout(2000)

        filled = fill_form(page)
        resume_ok = attach_resume(page)

        print("\n--- Application prepped ---")
        for f in filled:
            print(f"  filled: {f}")
        print(f"  resume attached: {'yes' if resume_ok else 'NO — attach manually'}")
        print("\nReview the form, answer the remaining questions, and click Submit")
        print("yourself. Press Enter here when you're done to close the browser.")
        try:
            input()
        except (KeyboardInterrupt, EOFError):
            pass
        browser.close()


if __name__ == "__main__":
    main()
