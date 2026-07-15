# JobScout Autofill (Chrome extension)

Fills job applications **in your own logged-in Chrome** using the same answers
the automation uses. This is the fix for postings the headless browser can't
do — Google/Apple/Amazon SSO sign-ins, and anything you'd rather submit by
hand. Your own browser is a real human session, so SSO works and nothing is
bot-blocked.

## Install (one time, ~1 min)

1. Chrome → `chrome://extensions`
2. Toggle **Developer mode** (top-right) on
3. **Load unpacked** → select this `extension/` folder
4. (optional) pin it to the toolbar

The dashboard must be running (`localhost:8765`) — it serves the answers. It
already runs as a background agent, so normally there's nothing to start.

## Use

On any application form: click the JobScout toolbar icon, or press
**⌘⇧J** (Ctrl+Shift+J). It fills every field it recognizes, then you review
and submit. Reads the *same* rules as the automation (`/api/fill-spec`), so
updating `answers.json` updates the extension too — no reinstall.

## Limits

- Fills text, textareas, native dropdowns, and radio buttons. Custom
  type-to-search dropdowns (some Greenhouse/Ashby widgets) may still need a
  manual pick — it fills what it safely can.
- Never clicks Submit. Review first, always.
- Needs the local dashboard reachable; if it can't connect it says so.
