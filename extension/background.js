// Toolbar click or Cmd+Shift+J -> fetch the fill spec from the local
// dashboard, then run the filler in the current tab. Fetching happens here
// (service worker) so it isn't blocked by the job site's page CSP.
async function fill(tab) {
  if (!tab || !tab.id) return;
  let spec;
  try {
    const r = await fetch("http://localhost:8765/api/fill-spec");
    spec = await r.json();
    if (spec.error) throw new Error(spec.error);
  } catch (e) {
    await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: (msg) => alert("JobScout: " + msg),
      args: ["couldn't reach the dashboard at localhost:8765 — is it running?"],
    });
    return;
  }
  const [{ result }] = await chrome.scripting.executeScript({
    target: { tabId: tab.id, allFrames: true },
    func: runFiller,
    args: [spec],
  });
}

chrome.action.onClicked.addListener(fill);
chrome.commands.onCommand.addListener((cmd) => {
  if (cmd === "fill") chrome.tabs.query(
    { active: true, currentWindow: true }, ([t]) => fill(t));
});

// Injected into the page. Mirrors auto_apply.py's matching: text fields by
// label fragment; radios/selects by exact-then-word-boundary option match.
function runFiller(spec) {
  const norm = (s) => (s || "").toLowerCase().replace(/\s+/g, " ").trim();

  function labelFor(el) {
    // explicit label[for=id]
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) return norm(l.textContent);
    }
    // input wrapped in its own <label> — use that label's text only
    const wrap = el.closest("label");
    if (wrap) return norm(wrap.textContent);
    // aria-label on the field itself
    if (el.getAttribute && el.getAttribute("aria-label"))
      return norm(el.getAttribute("aria-label"));
    // fieldset/legend (radio groups)
    const fs = el.closest("fieldset");
    if (fs) {
      const lg = fs.querySelector("legend");
      if (lg) return norm(lg.textContent);
    }
    // nearby preceding label in the same container (bounded)
    let n = el;
    for (let d = 0; d < 4 && n; d++) {
      n = n.parentElement;
      if (!n) break;
      const l = n.querySelector(":scope > label, :scope > legend");
      if (l && !l.contains(el)) return norm(l.textContent);
    }
    return "";
  }

  const wordHit = (want, opt) => {
    want = want.toLowerCase(); opt = opt.toLowerCase();
    if (opt.trim() === want) return true;
    if (want.length <= 3)
      return new RegExp(`(?<![a-z])${want}(?![a-z])`).test(opt);
    return opt.includes(want);
  };

  function setValue(el, value) {
    const proto = el.tagName === "TEXTAREA"
      ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
    setter.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  let filled = 0;

  // --- text / textarea ---
  document.querySelectorAll(
    "input[type=text], input[type=email], input[type=tel], input:not([type]), textarea"
  ).forEach((el) => {
    if (el.value || el.offsetParent === null) return;
    const lab = labelFor(el);
    if (!lab) return;
    for (const [frags, val] of spec.text) {
      if (val && frags.some((f) => lab.includes(f))) { setValue(el, val); filled++; break; }
    }
  });

  // --- native <select> ---
  document.querySelectorAll("select").forEach((el) => {
    if (el.value && el.value !== "" || el.offsetParent === null) return;
    const lab = labelFor(el);
    if (!lab) return;
    const rule = spec.select.find((r) => r[0].some((f) => lab.includes(f)))
      || spec.choice.find((r) => r[0].some((f) => lab.includes(f)));
    if (!rule) return;
    for (const want of rule[1]) {
      const opt = [...el.options].find((o) => wordHit(want, o.textContent));
      if (opt) { el.value = opt.value; el.dispatchEvent(new Event("change", { bubbles: true })); filled++; break; }
    }
  });

  // --- radio groups ---
  const groups = {};
  document.querySelectorAll("input[type=radio]").forEach((el) => {
    (groups[el.name || labelFor(el)] ||= []).push(el);
  });
  Object.values(groups).forEach((radios) => {
    if (radios.some((r) => r.checked)) return;
    const fs = radios[0].closest("fieldset");
    const lg = fs && fs.querySelector("legend");
    const q = norm(lg ? lg.textContent : labelFor(radios[0]));
    const rule = spec.choice.find((r) => r[0].some((f) => q.includes(f)));
    if (!rule) return;
    // per-option label = the wrapping <label>'s text (minus the input)
    const optLabel = (r) => {
      const w = r.closest("label");
      return norm(w ? w.textContent : labelFor(r));
    };
    for (const want of rule[1]) {
      const hit = radios.find((r) => wordHit(want, optLabel(r)));
      if (hit) { hit.click(); filled++; break; }
    }
  });

  return filled;
}
