// Verbatim content script: scan fields, fill facts, flag the rest,
// notice corrections. It has no submit code and never will.

(() => {
  // Version-stamped guard: after an extension update, the popup injects
  // the new script into pages that still hold the old orphaned one, and
  // the new version must take over rather than bail out.
  const VERSION = "0.6.9";
  if (window.__jobToolkitLoaded === VERSION) return;
  window.__jobToolkitLoaded = VERSION;
  // The server address, for links that open the Verbatim page. All server
  // calls go through background.js; this is only for window.open. Set in
  // the popup's Settings, kept in chrome.storage.local as "serverUrl".
  let SERVER = "http://localhost:5053";
  try {
    chrome.storage?.local?.get("serverUrl").then(o => {
      if (o && o.serverUrl) SERVER = String(o.serverUrl).replace(/\/+$/, "");
    }).catch(() => {});
  } catch (e) { /* no storage here: keep the default */ }

  const FLAGGED_STYLE = "2px solid #d97706";
  let tracked = []; // {el, sig, key, filled}
  let fillId = null; // the server's id for this fill, for its fill log
  let filledAStep = false;
  // What the last fill left for you, for the panel's list: {el, label, why}.
  let lastNeeds = [];
  // Profile keys a field asked for that profile.json has no value for.
  let lastMissing = [];

  // ---------- field discovery ----------

  function visible(el) {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== "hidden";
  }

  // Text a widget shows about itself rather than about what it asks for.
  // Taking "Select..." as a label is how every combobox on a page ended up
  // sharing one identity. Mirrors autofill.py:_WIDGET_WORDS.
  const WIDGET_WORDS =
    /^(select|search|choose|type here|please select|none|-+)\W*$/i;

  function usable(text) {
    const t = (text || "").trim();
    return t && !WIDGET_WORDS.test(t) ? t : "";
  }

  function labelFor(el) {
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (usable(l && l.textContent)) return l.textContent.trim();
    }
    const wrap = el.closest("label");
    if (usable(wrap && wrap.textContent)) return wrap.textContent.trim();
    const aria = usable(el.getAttribute("aria-label"));
    if (aria) return aria;
    const labelled = el.getAttribute("aria-labelledby");
    if (labelled) {
      const t = labelled.split(/\s+/)
        .map(id => document.getElementById(id)?.textContent || "")
        .join(" ").trim();
      if (usable(t)) return t;
    }
    // A custom widget usually sits in a field container next to a real
    // label element. Prefer that over the container's whole text, which
    // is where option lists and validation messages leak in.
    // While the container holds no other field it is this field's own
    // question block, and its label is the question however long it is.
    // Past that, a label only counts when no other field sits between it
    // and this one, so the question above never lends its words.
    const fieldSel = "input:not([type='hidden']), select, textarea";
    const after = (a, b) =>
      !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
    let host = el.parentElement;
    for (let i = 0; i < 4 && host && host.tagName !== "FORM"; i++) {
      const others = [...host.querySelectorAll(fieldSel)]
        .filter(x => x !== el && !el.contains(x) && visible(x));
      const labs = [...host.querySelectorAll("label, [class*='label'], [id$='-label']")]
        .filter(l => !l.contains(el) && usable(l.textContent));
      const own = !others.length;
      const lab = own ? labs[0] : labs.filter(l => after(l, el) &&
        !others.some(x => after(l, x) && after(x, el))).pop();
      if (lab && (own || lab.textContent.trim().length < 120)) {
        return lab.textContent.trim().replace(/\s+/g, " ").slice(0, 300);
      }
      host = host.parentElement;
    }
    // nearest preceding text, the last-resort signal; a select's
    // own option text is not a label, so subtract the element's own text
    let node = el.closest("div,fieldset,td,li") || el.parentElement;
    if (node) {
      const own = (el.tagName === "SELECT" ? el.textContent : el.value) || "";
      const t = node.textContent.replace(own, "").replace(el.value || "", "").trim();
      if (t && t.length < 120) return t;
    }
    // table layouts (Taleo and friends): the text lives in a sibling cell
    for (const host of [el, el.parentElement, el.closest("td")]) {
      const prev = host && host.previousElementSibling;
      if (prev) {
        const t = prev.textContent.trim();
        if (t && t.length < 120) return t;
      }
    }
    return "";
  }

  // Native controls, plus the ARIA widgets the vendors build instead of
  // them: react-select on Greenhouse, listbox buttons on Workday. All
  // three vendors are ARIA-labelled, so roles travel further than
  // vendor-specific selectors would.
  const WIDGET_SELECTOR = [
    "[role='combobox']", "[role='listbox']", "[role='radiogroup']",
    "[aria-haspopup='listbox']", "[contenteditable='true']",
  ].join(",");

  function widgetKind(el) {
    const role = (el.getAttribute("role") || "").toLowerCase();
    if (role === "radiogroup") return "radiogroup";
    if (el.isContentEditable) return "contenteditable";
    if (role === "combobox" || el.getAttribute("aria-haspopup") === "listbox"
        || role === "listbox") {
      // A combobox you can type into behaves differently from one you can
      // only open and pick from, so the filler needs to tell them apart.
      const typable = el.tagName === "INPUT" ||
        el.querySelector("input:not([type='hidden'])");
      return typable ? "combobox" : "listbox";
    }
    return "";
  }

  // ---------- grouped questions: radio sets and yes/no button pairs ----------
  //
  // A radio set is one question, so it travels as one field labelled with
  // the question, not one field per option labelled "Male" or "LinkedIn".
  // Ashby draws yes/no questions as two buttons over a hidden checkbox,
  // which the plain input scan never saw at all.

  const YESNO_SELECTOR = ".ashby-application-form-input-yesno, [class*='yesno']";

  function isOptionLabel(l) {
    if (l.tagName !== "LABEL") return false;
    const f = l.htmlFor && document.getElementById(l.htmlFor);
    return !!(f && (f.type === "radio" || f.type === "checkbox"))
      || !!l.querySelector("input[type='radio'], input[type='checkbox']");
  }

  // The question a group of options answers: an ARIA name, a legend, or
  // the nearest label-like text before the group, never an option's own
  // label.
  function questionFor(box, anchor = box) {
    const rg = box.closest("[role='radiogroup']");
    if (rg) {
      const by = rg.getAttribute("aria-labelledby");
      const t = by ? by.split(/\s+/).map(id =>
        document.getElementById(id)?.textContent || "").join(" ").trim() : "";
      if (usable(t)) return t;
      if (usable(rg.getAttribute("aria-label"))) return rg.getAttribute("aria-label").trim();
    }
    const lg = box.closest("fieldset")?.querySelector("legend");
    if (lg && usable(lg.textContent)) return lg.textContent.trim();
    const CANDS = "legend, label, [class*='label'], [class*='question-title'], "
      + "[class*='question'] > .text, h3, h4, p";
    const follows = (a, b) =>
      !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
    // Start at the group itself: its question is often inside it, above
    // the first option (a fieldset with a title label).
    let host = box;
    for (let i = 0; i < 6 && host && host.tagName !== "FORM" &&
         host !== document.body; i++) {
      // Other fields in this block. A text with one of them between it and
      // the group belongs to that field ("Phone" above a phone box, then a
      // consent question), so it is never this group's question.
      const others = [...host.querySelectorAll(
        "input:not([type='hidden']), select, textarea, button")]
        .filter(x => !box.contains(x));
      // the last label-like text that comes before the first option
      const before = [...host.querySelectorAll(CANDS)].filter(c =>
        !c.contains(anchor) && !isOptionLabel(c) &&
        !c.querySelector("input, button, select, textarea") &&
        follows(c, anchor) && usable(c.textContent) &&
        !others.some(x => follows(c, x) && follows(x, anchor)));
      // A title beats a paragraph of help text under it.
      const titles = before.filter(c => c.tagName !== "P");
      const pick = (titles.length ? titles : before).pop();
      if (pick) {
        return pick.textContent.trim()
          .replace(/\s+/g, " ").slice(0, 400);
      }
      host = host.parentElement;
    }
    return "";
  }

  function commonAncestor(els) {
    let node = els[0].parentElement;
    while (node && !els.every(e => node.contains(e))) node = node.parentElement;
    return node || document.body;
  }

  function optionText(input) {
    const l = (input.id && document.querySelector(`label[for="${CSS.escape(input.id)}"]`))
      || input.closest("label");
    return ((l && l.textContent) || input.value || input.getAttribute("aria-label") || "").trim();
  }

  // Ashby-style yes/no boxes, outermost only, each with real Yes/No buttons.
  function yesNoBoxes() {
    return [...document.querySelectorAll(YESNO_SELECTOR)]
      .filter(c => {
        const bs = [...c.querySelectorAll("button")];
        return bs.length >= 2 && bs.some(b => /^yes$/i.test(b.textContent.trim()))
          && bs.some(b => /^no$/i.test(b.textContent.trim()));
      })
      .filter(c => !c.parentElement || !c.parentElement.closest(YESNO_SELECTOR));
  }

  // Radio sets, one per name (or per radiogroup when unnamed). A set counts
  // as on screen when any option or its label is.
  function radioGroups() {
    const sets = new Map();
    for (const r of document.querySelectorAll("input[type='radio']")) {
      const key = r.name || r.closest("[role='radiogroup'], fieldset") || r;
      if (!sets.has(key)) sets.set(key, []);
      sets.get(key).push(r);
    }
    return [...sets.values()].filter(rs => rs.some(r =>
      visible(r) || visible(r.closest("label") || r.parentElement)));
  }

  // "Select all that apply" sets: checkboxes sharing a name, or several
  // checkboxes in one question block (Ashby names each box after its
  // option). A lone checkbox (a consent box) stays a field of its own.
  const BLOCK_SELECTOR = ".ashby-application-form-field-entry, fieldset, "
    + "[role='group'], .application-question";

  function checkboxSets() {
    const all = [...document.querySelectorAll("input[type='checkbox']")]
      .filter(c => !c.closest(YESNO_SELECTOR));
    const byName = new Map();
    for (const c of all) {
      if (!c.name) continue;
      if (!byName.has(c.name)) byName.set(c.name, []);
      byName.get(c.name).push(c);
    }
    const sets = new Map();
    for (const c of all) {
      let key = null;
      if (c.name && byName.get(c.name).length > 1) key = "name:" + c.name;
      else {
        const block = c.closest(BLOCK_SELECTOR);
        if (block && block.querySelectorAll("input[type='checkbox']").length > 1) key = block;
      }
      if (!key) continue;
      if (!sets.has(key)) sets.set(key, []);
      sets.get(key).push(c);
    }
    return [...sets.values()].filter(cs => cs.length > 1 && cs.some(c =>
      visible(c) || visible(c.closest("label") || c.parentElement)));
  }

  function collectFields() {
    const yesno = yesNoBoxes();
    const sets = checkboxSets();
    const inSet = new Set(sets.flat());
    const inYesNo = el => yesno.some(b => b.contains(el));
    const groups = radioGroups();
    const native = [...document.querySelectorAll("input, textarea, select")]
      .filter(el => !["hidden", "submit", "button", "file", "password",
                      "image", "reset", "radio"].includes(el.type))
      .filter(el => !inSet.has(el))
      // react-select's hidden "required" mirror: not a field, and filling
      // it only adds a second "did not keep the value" line.
      .filter(el => !(el.getAttribute("aria-hidden") === "true" && el.tabIndex === -1))
      .filter(el => !inYesNo(el));
    const widgets = [...document.querySelectorAll(WIDGET_SELECTOR)]
      // An open dropdown's own list carries role=listbox. It is the popup,
      // not a field, and it disappears again when the dropdown closes.
      .filter(el => !el.querySelector("[role='option']"))
      // A react-select renders an input inside its combobox wrapper. Keep
      // the innermost one only, or the same field is planned for twice.
      .filter(el => !wrapsAnInput(el))
      // A radiogroup of real radios is covered by its radio set.
      .filter(el => !el.querySelector("input[type='radio']"))
      .filter(el => !inYesNo(el));

    const entries = [...new Set([...native, ...widgets])].filter(visible)
      .map(el => ({ el, at: el }))
      .concat(yesno.filter(visible).map(b => ({ el: b, at: b, yesno: true })))
      .concat(groups.map(rs => ({ el: commonAncestor(rs), at: rs[0], radios: rs })))
      .concat(sets.map(cs => ({ el: commonAncestor(cs), at: cs[0], checks: cs })));
    entries.sort((a, b) => a.at === b.at ? 0
      : (a.at.compareDocumentPosition(b.at) & Node.DOCUMENT_POSITION_FOLLOWING) ? -1 : 1);

    return entries.map((e, i) => {
      const el = e.el;
      if (e.yesno) {
        const cb = el.querySelector("input");
        return {
          el, yesno: true,
          descriptor: {
            index: i, tag: "div", type: "", widget: "yesno", row: rowOf(el),
            name: (cb && cb.name) || "", id: "", placeholder: "",
            label: questionFor(el).slice(0, 150), options: ["Yes", "No"],
          },
        };
      }
      if (e.radios) {
        const first = e.radios[0];
        return {
          el, radios: e.radios,
          descriptor: {
            index: i, tag: "input", type: "radio", widget: "radiogroup",
            row: rowOf(first), name: first.name || "", id: "", placeholder: "",
            label: questionFor(el, first).slice(0, 150),
            options: e.radios.map(optionText).slice(0, 20),
          },
        };
      }
      if (e.checks) {
        const first = e.checks[0];
        return {
          el, checks: e.checks,
          descriptor: {
            index: i, tag: "input", type: "checkbox", widget: "checkboxgroup",
            row: rowOf(first), name: first.name && e.checks.every(c => c.name === first.name)
              ? first.name : "", id: "", placeholder: "",
            label: questionFor(el, first).slice(0, 150),
            options: e.checks.map(optionText).slice(0, 20),
          },
        };
      }
      const kind = widgetKind(el);
      return {
        el,
        descriptor: {
          index: i,
          tag: el.tagName.toLowerCase(),
          type: el.type || "",
          widget: kind || undefined,
          row: rowOf(el),
          name: el.name || el.getAttribute("name") || "",
          id: el.id || "",
          placeholder: el.placeholder || el.getAttribute("placeholder") || "",
          label: labelFor(el).slice(0, 150),
          options: el.tagName === "SELECT"
            ? [...el.options].map(o => o.textContent.trim()).slice(0, 20)
            : undefined,
        },
      };
    });
  }

  // Which block of a repeating section a field sits in. Education and work
  // history repeat the same questions once per school or job, so "School"
  // on its own does not say which school. Most vendors number the field
  // itself (school--0, education[1][school], degree_2), which is the
  // reliable signal. Failing that, count identical sibling blocks.
  function rowOf(el) {
    const attrs = [el.name, el.id, el.getAttribute("data-automation-id") || ""]
      .join(" ");
    const m = attrs.match(/(?:--|\[|_|-)(\d{1,2})(?:\]|\b)/);
    if (m) return Math.min(parseInt(m[1], 10), 20);

    // No number anywhere: find the repeating block this field lives in by
    // looking for an ancestor with same-shaped siblings. The block has to
    // hold several fields, or the first match is the wrapper around this
    // one field, whose siblings are the other fields in the same block.
    let node = el.parentElement;
    for (let i = 0; i < 6 && node && node.tagName !== "FORM"; i++) {
      const siblings = [...(node.parentElement?.children || [])]
        .filter(s => s.tagName === node.tagName && s.className === node.className);
      const fields = node.querySelectorAll("input, textarea, select").length;
      if (siblings.length > 1 && fields > 1) {
        return Math.min(siblings.indexOf(node), 20);
      }
      node = node.parentElement;
    }
    return 0;
  }

  // A widget wrapper whose own input is also in the list is redundant.
  function wrapsAnInput(el) {
    if (el.tagName === "INPUT" || el.tagName === "TEXTAREA") return false;
    return !!el.querySelector("input:not([type='hidden']), textarea");
  }

  // signature must mirror autofill.py:signature
  function signature(d) {
    const sig = [d.label, d.name, d.id, d.placeholder]
      .map(p => (p || "").trim().toLowerCase()).join("|")
      .replace(/\s+/g, " ").slice(0, 200);
    return sig || `tag:${d.tag}`;
  }

  // ---------- filling ----------

  // ---- phone format ----
  // The stored number, written the way this field asks: the example in its
  // placeholder or help text (separators mirrored), its pattern or length,
  // and whether a country code sits beside it (a +1 prefix, a code picker)
  // so only the local number goes in. With no hint, the stored value as is.
  const SLOT = /[0-9xX#_]/;
  function phoneHint(el) {
    const ids = (el.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean);
    const described = ids.map(id => document.getElementById(id)?.textContent || "").join(" ");
    const box = el.closest("[class*=field], [class*=Field], [class*=question], li, fieldset") || el.parentElement;
    const help = box ? [...box.querySelectorAll("small, .help, [class*=hint], [class*=help], [class*=description]")]
      .filter(x => !x.contains(el)).map(x => x.textContent).join(" ") : "";
    return [labelFor(el), el.placeholder, described, help, el.title].join(" ");
  }
  function codeBeside(el) {
    // A country code shown or picked next to the box: an intl-tel-input
    // flag, a "+1" prefix, a dial code select, or Workday's own field.
    for (let n = el.parentElement, i = 0; n && i < 3; n = n.parentElement, i++) {
      if (n.querySelector("[class*=iti__], [class*=dial], [class*=country-code], [class*=countryCode]")) return n;
      const pre = [...n.querySelectorAll("span, div, button")].find(x => !x.contains(el) && /^\s*\+\d{1,3}\s*$/.test(x.textContent));
      if (pre) return n;
      const sel = [...n.querySelectorAll("select")].find(x => x !== el && /\+\d{1,3}|dial|country code/i.test(x.textContent + " " + labelFor(x)));
      if (sel) return sel;
    }
    const wd = [...document.querySelectorAll("label, [data-automation-id]")]
      .find(x => /country phone code/i.test(x.textContent || "") && x.textContent.length < 60);
    return wd || null;
  }
  function setUsCode(sel) {
    if (!sel || sel.tagName !== "SELECT") return;
    if (/\+1\b|united states/i.test(sel.selectedOptions[0]?.textContent || "")) return;
    const opt = [...sel.options].find(o => /united states/i.test(o.textContent) && !/minor|outlying/i.test(o.textContent))
      || [...sel.options].find(o => /(^|[^\d])\+1(\D|$)/.test(o.textContent) || /^(US|\+?1)$/i.test(o.value));
    if (opt) { sel.value = opt.value; sel.dispatchEvent(new Event("change", { bubbles: true })); }
  }
  function mirror(template, digits) {
    let i = 0, out = "";
    for (const c of template) {
      if (SLOT.test(c)) { if (i >= digits.length) break; out += digits[i++]; }
      else out += c;
    }
    return i === digits.length ? out.trim() : "";
  }
  function formatPhone(el, raw) {
    const all = (raw || "").replace(/\D/g, "");
    const local = all.length === 11 && all[0] === "1" ? all.slice(1) : all;
    if (local.length !== 10) return raw;
    const code = codeBeside(el);
    if (code) setUsCode(code);
    const bare = raw.replace(/^\s*\+?1[\s.-]+/, "").trim();
    // A pattern or length that only fits ten digits.
    const pat = el.getAttribute("pattern") || "";
    if ((el.maxLength > 0 && el.maxLength <= 10) || /^\^?(\\d|\[0-9\])\{10\}\$?$/.test(pat)) return local;
    const fromPat = pat.replace(/\\d|\[0-9\]/g, "0").replace(/0\{(\d+)\}/g, (m, n) => "0".repeat(+n))
      .replace(/\\([().\s-])/g, "$1").replace(/[\^$]/g, "");
    const hint = phoneHint(el);
    const ex = (hint.match(/(\+\d{1,3}[\s.-]?)?\(?[0-9xX#]{3}\)?[\s.-]?[0-9xX#]{3}[\s.-]?[0-9xX#]{4}/) || [])[0]
      || (/^[0-9().\s+-]{10,}$/.test(fromPat) ? fromPat : "");
    if (ex) {
      const cc = ex.match(/^\+(\d{1,3})[\s.-]?/);
      const rest = cc ? ex.slice(cc[0].length) : ex;
      const mine = mirror(rest, local);
      if (mine) return cc && !code ? `+1${cc[0].slice(cc[1].length + 1)}${mine}` : mine;
    }
    if (/international|country code|\+1/i.test(hint) && !code)
      return `+1 ${local.slice(0, 3)} ${local.slice(3, 6)} ${local.slice(6)}`;
    // A code beside the box: the stored number without its own +1.
    return code ? bare : raw;
  }

  function setNativeValue(el, value) {
    const proto = el.tagName === "TEXTAREA"
      ? window.HTMLTextAreaElement.prototype
      : window.HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
    setter.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function fillSelect(el, value) {
    const want = value.trim().toLowerCase();
    let opt = [...el.options].find(o => o.textContent.trim().toLowerCase() === want)
      || bestOption([...el.options].filter(o => o.value !== ""), value);
    if (!opt) {
      opt = [...el.options].find(o => {
        const t = o.textContent.trim().toLowerCase();
        return t.length > 2 && (t.includes(want) || want.includes(t));
      });
    }
    if (!opt) return false;
    // Through the native setter, like the inputs: React keeps its own copy
    // of the last value on the element, and a plain assignment leaves that
    // copy stale, so the change event is ignored and the value reverts.
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLSelectElement.prototype, "value").set;
    setter.call(el, opt.value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return el.value === opt.value;
  }

  // ---------- custom widgets (react-select, Workday listboxes) ----------

  const sleep = ms => new Promise(r => setTimeout(r, ms));

  // Widgets open their option list asynchronously. Poll briefly rather
  // than guessing a delay that is always wrong on one machine or another.
  async function waitFor(fn, ms = 1200, step = 60) {
    for (let waited = 0; waited < ms; waited += step) {
      const v = fn();
      if (v) return v;
      await sleep(step);
    }
    return null;
  }

  function matchText(text, want) {
    const t = (text || "").trim().toLowerCase();
    const w = want.trim().toLowerCase();
    return t === w || (t.length > 2 && (t.includes(w) || w.includes(t)));
  }

  function openOptions(el) {
    // Only options that belong to this widget: its aria-controls or
    // aria-owns list when it names one, otherwise whatever list is open.
    const id = el.getAttribute("aria-controls") || el.getAttribute("aria-owns");
    const scope = (id && document.getElementById(id)) || document;
    const opts = [...scope.querySelectorAll("[role='option']")].filter(visible);
    return opts.length ? opts : null;
  }

  // react-select (Greenhouse and others) keeps its picked value in a
  // single-value element beside the input and clears the input itself.
  function selectControl(el) {
    const byPrefix = el.closest("[class*='select__control']");
    if (byPrefix) return byPrefix;
    // unprefixed react-select: emotion classes like "css-13cymwt-control"
    for (let n = el.parentElement, i = 0; n && i < 5; n = n.parentElement, i++) {
      if (/(^|\s)css-[\w]+-control(\s|$)/.test(n.className || "")) return n;
    }
    return null;
  }

  // What the widget now shows, for reading back what a click actually did.
  function widgetText(el) {
    if (el.tagName === "INPUT") {
      const ctl = selectControl(el);
      const shown = ctl && ctl.querySelector(
        "[class*='single-value'], [class*='singleValue'], [class*='multi-value__label']");
      if (shown) return shown.textContent.trim();
      return el.value || "";
    }
    const owner = el.closest("[class*='control'],[class*='select'],div") || el;
    return (owner.innerText || owner.textContent || "").trim();
  }

  // Comparable text: case, punctuation and "do not"/"don't" do not matter.
  function canon(t) {
    return (t || "").normalize("NFD").replace(/[̀-ͯ]/g, "")
      .toLowerCase().replace(/[‘’']/g, "")
      .replace(/\bdo not\b/g, "dont").replace(/\bam not\b/g, "amnot")
      .replace(/[^a-z0-9+]+/g, " ").trim();
  }

  // The one option a value clearly means, or null. Exact first, then an
  // option that starts with the value as whole words ("Yes" -> "Yes - I
  // consent", "Asian" -> "Asian (Not Hispanic or Latino)"), then for long
  // values only, an option the value starts with. Two candidates at the
  // same level means it is not clear, so nothing is picked.
  function bestOption(items, value, textOf = x => x.textContent) {
    const w = canon(value);
    if (!w) return null;
    // The same option listed twice (a phone country picker pins "United
    // States +1" above the A to Z list) is one choice, not a tie.
    const seenText = new Set();
    items = items.filter(o => {
      const t = canon(textOf(o));
      if (seenText.has(t)) return false;
      seenText.add(t);
      return true;
    });
    const tiers = [
      t => t === w,
      t => t.startsWith(w + " "),
      t => w.length > 12 && t.length > 12 && w.startsWith(t + " "),
    ];
    for (const test of tiers) {
      const hits = items.filter(o => test(canon(textOf(o))));
      if (hits.length === 1) return hits[0];
      if (hits.length > 1) return null;
    }
    // A number of years against ranges: "8" -> "6-8 years", "5+ years".
    if (/^\d+$/.test(w)) {
      const hit = rangeOption(items, parseInt(w, 10), textOf);
      if (hit) return hit;
    }
    // The same answer in the other common wording, or the broader answer
    // when a list has no detailed option ("Southeast Asian" -> "Asian").
    const aliases = { male: ["man"], female: ["woman"],
                      "southeast asian": ["south east asian", "asian"] }[w] || [];
    for (const alias of aliases) {
      const hit = bestOption(items, alias, textOf);
      if (hit) return hit;
    }
    return meaningOption(items, value, textOf) || placeOption(items, value, textOf);
  }

  // The profile key of the field being filled, so option matching can use
  // the answer's meaning as well as its words.
  let currentKey = "";

  // Common stored answers in the other wordings forms use for them, most
  // preferred first. Each entry applies only when the stored answer says
  // what "when" expects. A pattern must match the whole option (lower
  // case, punctuation as spaces, "do not" as "dont"); a decline option is
  // never picked, and two options matching one pattern pick nothing.
  const MEANINGS = [
    { key: "eeo_gender", when: /^male$/,
      accept: [/^male$/, /^man$/, /^man male or masculine$/, /^male man$/,
               /^cisgender man$/, /^cisgender male$/] },
    { key: "eeo_gender", when: /^female$/,
      accept: [/^female$/, /^woman$/, /^woman female or feminine$/, /^female woman$/,
               /^cisgender woman$/, /^cisgender female$/] },
    { key: "eeo_veteran", when: /(amnot|not) a protected veteran/,
      accept: [/^i amnot a protected veteran$/, /^not a protected veteran$/,
               /^no i amnot a protected veteran$/,
               /^i amnot a veteran( i did not serve in the military)?$/,
               /^i have never served in the military$/,
               /^no i amnot a veteran$/, /^i dont identify as a protected veteran$/,
               /^i dont fall into (one of )?the above categories of protected veterans?$/,
               /^no$/] },
    { key: "eeo_disability", when: /^no\b/,
      accept: [/^no i dont have a disability\b/, /^i dont have a disability\b/,
               /^no$/, /^none of (these|the above)( apply)?$/] },
    { key: "eeo_orientation", when: /^(heterosexual|straight)$/,
      accept: [/^heterosexual$/, /^straight$/, /^straight heterosexual$/,
               /^heterosexual straight$/, /^heterosexual or straight$/,
               /^straight or heterosexual$/] },
    { key: "eeo_transgender", when: /^no$/,
      accept: [/^no$/, /^i amnot transgender$/, /^not transgender$/] },
    { key: "eeo_lgbtq", when: /^no$/,
      accept: [/^no$/, /^i amnot a member of the lgbtq\w* community$/] },
    { key: "eeo_hispanic", when: /^no$/,
      accept: [/^no$/, /^not hispanic( or latin[oaex]| latin[oaex])?$/,
               /^no i amnot hispanic( or latin[oaex])?$/] },
    // Education, per stored entry. Forms list schools and majors in their
    // own words, so add an entry for yours: "when" matches the school or
    // major as written in profile.json, "accept" lists the wordings a form
    // may use, most preferred first. A school inside a university is that
    // university; for a major, the major itself, then the nearest broader
    // majors, then "Other". The school entry below is an invented example.
    { key: "school", when: /^example university school of information$/,
      accept: [/^example university school of information$/,
               /^example university$/, /^example university main campus$/] },
    { key: "discipline", when: /^human computer interaction$/,
      accept: [/^human computer interaction$/, /^hci$/, /^human computer interaction hci$/,
               /^information$/, /^information science$/, /^information sciences$/,
               /^information systems$/, /^other$/] },
    { key: "discipline", when: /^interior architecture (and )?design$/,
      accept: [/^interior architecture (and )?design$/, /^interior design$/,
               /^design$/, /^other$/] },
    { key: "country", when: /^united states( of america)?$/,
      accept: [/^united states( \+?1)?$/, /^united states of america( \+?1)?$/, /^usa$/] },
  ];
  const DECLINE = /\b(decline|dont wish|prefer not|not to answer|self describe|not listed)\b/;

  function meaningOption(items, value, textOf = x => x.textContent) {
    const w = canon(value);
    const rule = MEANINGS.find(m => m.key === currentKey && m.when.test(w));
    if (!rule) return null;
    const options = items.filter(o => !DECLINE.test(canon(textOf(o))));
    for (const rx of rule.accept) {
      const hits = options.filter(o => rx.test(canon(textOf(o))));
      if (hits.length === 1) return hits[0];
      if (hits.length > 1) return null;
    }
    return null;
  }

  // Schools and majors: the exact name, or what the MEANINGS table above
  // accepts for that entry. Nothing looser.
  function eduOption(items, key, value, textOf = x => x.textContent) {
    const w = canon(value);
    const seen = new Set();
    const exact = items.filter(o => {
      const t = canon(textOf(o));
      if (t !== w || seen.has(t)) return false;
      seen.add(t);
      return true;
    });
    if (exact.length === 1) return exact[0];
    return meaningOption(items, value, textOf);
  }

  // What to type into a school or major search box: the distinctive part,
  // since these lists search on it ("Example" for "University of Example").
  function searchTerm(key, value) {
    if (key === "school") {
      const m = value.match(/^\s*university of (.+?)(\s+(school|college|institute)\b.*)?$/i);
      if (m) return m[1];
      return value.replace(/\s+(university|college|institute)\b.*$/i, "") || value;
    }
    if (key === "discipline") return canon(value).split(" ").slice(0, 2).join(" ");
    return value;
  }

  // Whether a number fits an option's range, or null when the option is
  // not a range at all.
  function inRange(text, n) {
    const t = (text || "").toLowerCase().replace(/[\u2013\u2014]/g, "-");
    let m;
    if ((m = t.match(/(\d+)\s*(?:-|to)\s*(\d+)/))) return n >= +m[1] && n <= +m[2];
    if ((m = t.match(/(\d+)\s*\+/)) || (m = t.match(/(\d+)\s*(?:years?\s*)?(?:or more|and above|and up|or greater|plus)/)))
      return n >= +m[1];
    if ((m = t.match(/(?:at least|minimum of)\s*(\d+)/))) return n >= +m[1];
    if ((m = t.match(/(?:more than|over|greater than)\s*(\d+)/))) return n > +m[1];
    if ((m = t.match(/(?:less than|under|fewer than)\s*(\d+)/))) return n < +m[1];
    if ((m = t.match(/^\s*(\d+)\s*(?:years?)?\s*$/))) return n === +m[1];
    return null;
  }

  function rangeOption(items, n, textOf = x => x.textContent) {
    const hits = items.filter(o => inRange(textOf(o), n) === true);
    return hits.length === 1 ? hits[0] : null;
  }

  // A yes/no question that asks about a threshold ("Do you have more than
  // 6 years ...?") answered from a stored number: the number decides.
  function thresholdAnswer(label, value) {
    if (!/^\s*\d+(\.\d+)?\s*$/.test(value || "") || !/\byears?\b/i.test(label || "")) return null;
    const ok = inRange(label, parseFloat(value));
    return ok === null ? null : (ok ? "Yes" : "No");
  }

  const US_STATES = {
    AL: "Alabama", AK: "Alaska", AZ: "Arizona", AR: "Arkansas", CA: "California",
    CO: "Colorado", CT: "Connecticut", DE: "Delaware", DC: "District of Columbia",
    FL: "Florida", GA: "Georgia", HI: "Hawaii", ID: "Idaho", IL: "Illinois",
    IN: "Indiana", IA: "Iowa", KS: "Kansas", KY: "Kentucky", LA: "Louisiana",
    ME: "Maine", MD: "Maryland", MA: "Massachusetts", MI: "Michigan",
    MN: "Minnesota", MS: "Mississippi", MO: "Missouri", MT: "Montana",
    NE: "Nebraska", NV: "Nevada", NH: "New Hampshire", NJ: "New Jersey",
    NM: "New Mexico", NY: "New York", NC: "North Carolina", ND: "North Dakota",
    OH: "Ohio", OK: "Oklahoma", OR: "Oregon", PA: "Pennsylvania",
    RI: "Rhode Island", SC: "South Carolina", SD: "South Dakota",
    TN: "Tennessee", TX: "Texas", UT: "Utah", VT: "Vermont", VA: "Virginia",
    WA: "Washington", WV: "West Virginia", WI: "Wisconsin", WY: "Wyoming",
  };

  // "San Francisco, CA" against a place list that spells the state out:
  // "San Francisco, California, United States". The city must be the whole
  // first part, so "South San Francisco" never counts.
  function placeOption(items, value, textOf = x => x.textContent) {
    const [city, st] = (value || "").split(",").map(s => s.trim());
    if (!city || !st) return null;
    const states = [canon(st), canon(US_STATES[st.toUpperCase()] || "")].filter(Boolean);
    const hits = items.filter(o => {
      const parts = textOf(o).split(",").map(canon);
      return parts[0] === canon(city) && states.includes(parts[1] || "");
    });
    return hits.length === 1 ? hits[0] : null;
  }

  const mouse = (el, type) => el.dispatchEvent(new MouseEvent(type,
    { bubbles: true, cancelable: true, view: window, button: 0 }));

  async function fillWidget(el, kind, value) {
    if (kind === "radiogroup") return false; // only real radio sets are filled
    if (kind === "contenteditable") {
      el.focus();
      el.textContent = value;
      el.dispatchEvent(new InputEvent("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
      return (el.textContent || "").includes(value);
    }

    el.scrollIntoView({ block: "center" });
    const typable = el.tagName === "INPUT" ? el
      : el.querySelector("input:not([type='hidden'])");
    const ctl = typable && kind === "combobox" ? selectControl(typable) : null;

    if (ctl) {
      // react-select opens on a click on its control, and only while the
      // input does not have focus yet. A click on the input does nothing.
      if (document.activeElement === typable) typable.blur();
      for (const t of ["mousedown", "mouseup", "click"]) mouse(ctl, t);
    } else {
      mouse(el, "mousedown");
      (typable || el).focus();
      el.click();
    }

    // Leftover search text makes an empty box look answered. Whatever
    // happens next, a failed pick must not leave it behind.
    const giveUp = () => {
      el.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true,
                                                      key: "Escape" }));
      if (typable && kind === "combobox" && typable.value &&
          !matchText(widgetText(el), value)) setNativeValue(typable, "");
      if (ctl && typable) typable.blur();
      return false;
    };

    let pick = null;
    if (typable && kind === "combobox") {
      if (ctl) await waitFor(() => openOptions(typable), 800);
      // Typing narrows the list, which is what makes long option lists
      // (countries, schools) usable at all.
      const edu = currentKey === "school" || currentKey === "discipline";
      setNativeValue(typable, edu ? searchTerm(currentKey, value) : value);
      typable.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true,
                                                           key: "a" }));
      // School and major lists are searched on the server: give them time.
      const opts = await waitFor(() => openOptions(el), edu ? 3000 : 1200);
      pick = opts && (edu ? eduOption(opts, currentKey, value) : (bestOption(opts, value) ||
        (ctl ? null : opts.find(o => matchText(o.textContent, value)))));
      if (!pick) {
        // The option may be worded differently from the value: clear the
        // filter and look through the whole list (for a major, that is where
        // "Information Science" or "Other" can be).
        setNativeValue(typable, "");
        const all = await waitFor(() => openOptions(el), edu ? 1500 : 600);
        pick = all && (edu ? eduOption(all, currentKey, value) : bestOption(all, value));
      }
    } else {
      const opts = await waitFor(() => openOptions(el));
      pick = opts && (bestOption(opts, value)
        || opts.find(o => matchText(o.textContent, value))
        || (typable ? null : opts.find(o => matchText(o.innerText, value))));
    }
    if (!pick) return giveUp();
    pick.scrollIntoView({ block: "nearest" });
    mouse(pick, "mousedown");
    pick.click();
    await sleep(120);

    // Read back. A widget fill is a sequence of clicks against code we do
    // not control, so believing it worked is not good enough: an unverified
    // wrong value on a real application is worse than an empty field.
    const picked = pick.textContent.trim();
    const ok = matchText(widgetText(el), picked) || matchText(widgetText(el), value) ||
           (typable && !ctl ? matchText(typable.value, value) : false);
    if (!ok) return giveUp();
    if (ctl && typable) typable.blur();
    return true;
  }

  // ---------- grouped questions ----------

  function yesNoOf(value) {
    const v = canon(value);
    if (/^(yes|y|true)\b/.test(v)) return "yes";
    if (/^(no|n|false)\b/.test(v)) return "no";
    return null;
  }

  async function fillYesNo(box, value) {
    const want = yesNoOf(value);
    if (!want) return false;
    const btn = box.querySelector(`button[data-option="${want}"]`)
      || [...box.querySelectorAll("button")].find(b => canon(b.textContent) === want);
    if (!btn) return false;
    if (btn.getAttribute("aria-pressed") !== "true") {
      btn.scrollIntoView({ block: "center" });
      btn.click();
      await waitFor(() => btn.getAttribute("aria-pressed") === "true", 800);
    }
    return btn.getAttribute("aria-pressed") === "true";
  }

  function yesNoValue(box) {
    const on = box.querySelector("button[aria-pressed='true']");
    return on ? on.textContent.trim() : "";
  }

  async function fillRadios(radios, value) {
    let r = bestOption(radios, value, optionText);
    // Yes/No questions often word their options out ("Yes, I am"), and the
    // stored answer is a bare Yes or No.
    if (!r && yesNoOf(value)) {
      const want = yesNoOf(value);
      const hits = radios.filter(x => yesNoOf(optionText(x)) === want);
      if (hits.length === 1) r = hits[0];
    }
    if (!r) return false;
    if (!r.checked) {
      const target = visible(r) ? r
        : (r.id && document.querySelector(`label[for="${CSS.escape(r.id)}"]`))
          || r.closest("label") || r;
      target.scrollIntoView({ block: "center" });
      target.click();
      await sleep(60);
    }
    return r.checked;
  }

  // Tick the options a stored answer clearly names. A list answer
  // ("English (fluent), Mandarin") ticks one box per part, and only when
  // every part has its box; otherwise nothing is ticked.
  async function fillChecks(boxes, value) {
    let picks = [];
    const one = bestOption(boxes, value, optionText);
    if (one) picks = [one];
    else if (value.includes(",")) {
      const parts = value.split(",").map(p => p.replace(/\(.*?\)/g, "").trim()).filter(Boolean);
      const hits = parts.map(p => bestOption(boxes, p, optionText));
      if (hits.length && hits.every(Boolean)) picks = [...new Set(hits)];
    }
    if (!picks.length) return false;
    for (const c of picks) {
      if (c.checked) continue;
      const target = visible(c) ? c
        : (c.id && document.querySelector(`label[for="${CSS.escape(c.id)}"]`))
          || c.closest("label") || c;
      target.scrollIntoView({ block: "center" });
      target.click();
      await sleep(60);
    }
    return picks.every(c => c.checked);
  }

  function checksValue(boxes) {
    return boxes.filter(c => c.checked).map(optionText).join(", ");
  }

  // Lever's location box searches as you type and only keeps a place picked
  // from its list: typing alone is wiped when the box loses focus.
  function isLeverLocation(el) {
    return el.matches && el.matches("input.location-input") &&
      !!document.getElementById("selected-location");
  }

  async function fillLeverLocation(el, value) {
    el.scrollIntoView({ block: "center" });
    el.focus();
    setNativeValue(el, value);
    el.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, key: "a" }));
    const opts = await waitFor(() => {
      const o = [...document.querySelectorAll(".dropdown-location")];
      return o.length ? o : null;
    }, 4000, 100);
    const pick = opts && (bestOption(opts, value) || placeOption(opts, value));
    if (!pick) {
      setNativeValue(el, "");
      el.blur();
      return false;
    }
    mouse(pick, "mousedown");
    await sleep(100);
    return matchText(el.value, pick.textContent.trim()) &&
      !!document.getElementById("selected-location").value;
  }

  // A question answered Yes or No: Ashby's button pair, radios or a select
  // whose options are Yes and No, or a dropdown whose label asks a question
  // (react-select lists stay unknown until opened).
  function yesNoField(f) {
    if (f.yesno) return true;
    const opts = f.radios ? f.radios.map(optionText)
      : f.el.tagName === "SELECT" ? [...f.el.options].map(o => o.textContent) : null;
    if (opts) {
      const yn = opts.map(yesNoOf).filter(Boolean);
      return yn.includes("yes") && yn.includes("no");
    }
    const kind = f.descriptor.widget;
    return (kind === "combobox" || kind === "listbox") &&
      /\?\s*\*?\s*$/.test(f.descriptor.label || "");
  }

  // "If yes, please explain" boxes belong to the question above them.
  const FOLLOW_UP = /^\W*(if (yes|so)\b|if you (answered|selected|said|chose)\W+yes\b)/i;

  function radiosValue(radios) {
    const on = radios.find(r => r.checked);
    return on ? optionText(on) : "";
  }

  // What a field holds now, whatever kind of field it is.
  function fieldValue(f) {
    const el = f.el;
    if (f.yesno) return yesNoValue(el);
    if (f.radios) return radiosValue(f.radios);
    if (f.checks) return checksValue(f.checks);
    if (el.tagName === "SELECT") return el.selectedOptions[0]?.textContent.trim() || "";
    return f.descriptor.widget ? widgetText(el) : (el.value || "");
  }

  function mark(el, style, title, label) {
    el.style.outline = style;
    el.style.outlineOffset = "1px";
    if (title) el.title = title;
    // Every amber outline is also a line in the panel's Needs you list,
    // one line per element even when two checks flag the same one.
    if (style === FLAGGED_STYLE && title && !lastNeeds.some(n => n.el === el)) {
      lastNeeds.push({ el, label: label || needLabel(el),
                       why: title.replace(/^Verbatim:\s*/, "")
                         .replace(/^./, c => c.toUpperCase()) });
    }
  }

  function needLabel(el) {
    const input = el.matches && el.matches("input, textarea, select, [role]")
      ? el : el.querySelector && el.querySelector('input[type="file"]');
    if (input && input.type === "file") {
      const k = fileKind(input);
      return k === "resume" ? "Resume upload"
        : k === "cover_letter" ? "Cover letter upload" : "File upload";
    }
    return (labelFor(el) || el.name || el.id || "A field")
      .replace(/\s*\*\s*$/, "").slice(0, 140);
  }

  // Tells the background a fill is running, so it never updates itself
  // in the middle of one.
  function tellBusy(on) {
    try {
      chrome.runtime.sendMessage({ type: "fill-busy", on }).catch(() => {});
    } catch (e) { /* old copy after an update; nothing to tell */ }
  }

  async function runFill() {
    tellBusy(true);
    try {
      return await runFillOnce();
    } finally {
      tellBusy(false);
    }
  }

  async function runFillOnce() {
    lastNeeds = [];
    lastMissing = [];
    tracked = [];
    fillId = null; // set by this fill's first plan call
    // attach documents first: some sites parse the resume and prefill
    // fields, and the verbatim fill below should win over their parsing
    const docs = await attachDocs();

    const fields = collectFields();
    if (!fields.length && !docs.uploaded && !docs.fileFlags) {
      return { error: "no form fields on this page" };
    }

    const first = await planAndApply(fields, fields);
    if (first.error) return first;
    let { filled, flagged, unidentified, mappingError } = first;
    let total = fields.length;

    // An answer can open more questions (the survey under a country, a
    // follow-up under a Yes). One more pass covers just the new fields,
    // once, so a page that keeps changing cannot loop.
    await sleep(700);
    const keyOf = f => (f.radios || f.checks || [f.el])[0];
    const known = new Set(fields.map(keyOf));
    // New and still empty: a field a re-render merely replaced keeps its
    // value and is not filled twice.
    const empty = f => f.el.tagName === "SELECT" ? !f.el.value : !fieldValue(f);
    const later = collectFields().filter(f => !known.has(keyOf(f)) && empty(f));
    if (later.length) {
      later.forEach((f, i) => { f.descriptor.index = i; });
      const both = fields.concat(later).sort((a, b) => a === b ? 0
        : (keyOf(a).compareDocumentPosition(keyOf(b)) & Node.DOCUMENT_POSITION_FOLLOWING) ? -1 : 1);
      const second = await planAndApply(later, both);
      if (!second.error) {
        filled += second.filled;
        flagged += second.flagged;
        unidentified += second.unidentified;
        total += later.length;
      }
    }
    watchCorrections();
    return { filled, flagged: flagged + docs.fileFlags,
             uploaded: docs.uploaded, docsFor: docs.docsFor,
             unidentified, mappingError,
             total };
  }

  // Ask the server for a plan for these fields and carry it out. "all" is
  // every field this fill has seen, for finding the question a follow-up
  // box belongs to.
  async function planAndApply(fields, all) {
    const resp = await chrome.runtime.sendMessage({
      type: "plan",
      payload: {
        domain: location.hostname,
        url: location.href,
        fields: fields.map(f => f.descriptor),
      },
    });
    if (!resp || !resp.ok) return { error: resp ? resp.error : "no response" };
    const { plan = [], unidentified = 0, mapping_error = null } = resp.data;
    // The second pass plans again; corrections stay logged under the
    // first pass's id, which is the fill the user started.
    fillId = fillId || resp.data.fill_id || null;

    let filled = 0, flagged = 0;
    const steps = [...plan].sort((a, b) => a.index - b.index);
    for (let step of steps) {
      const f = fields[step.index];
      if (!f) continue;
      const el = f.el;
      const kind = f.descriptor.widget;
      // A follow-up box ("If yes, please explain") under a question
      // answered No stays empty, with nothing to flag.
      if (FOLLOW_UP.test(f.descriptor.label || "")) {
        const prev = all.filter(o => o !== f && o.el.isConnected && !o.el.contains(f.el) &&
          (o.el.compareDocumentPosition(f.el) & Node.DOCUMENT_POSITION_FOLLOWING)).pop();
        if (prev && yesNoOf(fieldValue(prev)) === "no") continue;
      }
      if (step.action === "fill") {
        let ok = false;
        // A number answering a threshold question decides Yes or No.
        // Only on a yes/no question: a text box labelled "Years of experience
        // (5+ required)" keeps the number.
        const value = (yesNoField(f) && thresholdAnswer(f.descriptor.label, step.value))
          || step.value;
        step = { ...step, value: step.key === "phone" && el.tagName === "INPUT" && !kind
          ? formatPhone(el, value) : value };
        currentKey = step.key || "";
        // An element that went away while the plan was in flight cannot be
        // filled, and writing into it would report a success nobody can see.
        if (!el.isConnected) {
          flagged++;
          continue;
        }
        if (f.yesno || f.radios || f.checks) {
          // A grouped question is filled only when the stored answer clearly
          // names one of its options; anything less is the user's to pick.
          ok = f.yesno ? await fillYesNo(el, step.value)
            : f.radios ? await fillRadios(f.radios, step.value)
              : await fillChecks(f.checks, step.value);
          if (!ok) {
            mark(el, FLAGGED_STYLE, `Verbatim: no option clearly matched "${step.value}". Pick it yourself.`,
                 f.descriptor.label);
            flagged++;
            continue;
          }
        } else if (kind === "radiogroup" || el.type === "checkbox"
            || el.type === "radio") {
          mark(el, FLAGGED_STYLE, "Verbatim: check this one yourself.");
          flagged++;
          continue;
        } else if (kind) {
          ok = await fillWidget(el, kind, step.value);
          if (!ok) {
            mark(el, FLAGGED_STYLE,
                 `Verbatim: could not set this to "${step.value}". `
                 + "Pick it yourself.");
            flagged++;
            continue;
          }
        } else if (isLeverLocation(el)) {
          ok = await fillLeverLocation(el, step.value);
          if (!ok) {
            mark(el, FLAGGED_STYLE,
                 `Verbatim: no place in the list matched "${step.value}". Pick it yourself.`);
            flagged++;
            continue;
          }
        } else if (el.tagName === "SELECT") {
          ok = fillSelect(el, step.value);
          if (!ok) {
            mark(el, FLAGGED_STYLE,
                 `Verbatim: no option matched "${step.value}". Pick it yourself.`);
            flagged++;
            continue;
          }
        } else {
          setNativeValue(el, step.value);
          // Some forms only accept a value once the field is left, and
          // some reject it and revert; both show up in the read back.
          el.dispatchEvent(new FocusEvent("blur", { bubbles: true }));
          ok = (el.value || "") === step.value;
          if (!ok) {
            mark(el, FLAGGED_STYLE,
                 "Verbatim: this field did not keep the value. Type it "
                 + "yourself.");
            flagged++;
            continue;
          }
        }
        if (ok) {
          // No visual marker on filled fields, by choice: the values are
          // your own facts, and the popup reports the counts. Flags keep
          // their amber outline because those genuinely need your eyes.
          tracked.push({ el, sig: signature(f.descriptor), key: step.key,
                         filled: fieldValue(f) || step.value,
                         read: () => fieldValue(f) });
          filled++;
        }
      } else if (step.action === "flag") {
        if (step.reason === "No stored value for this." && step.key) {
          lastMissing.push(step.key);
        }
        mark(el, FLAGGED_STYLE, `Verbatim: ${step.reason}`,
             f.yesno || f.radios || f.checks ? f.descriptor.label : undefined);
        flagged++;
      }
    }
    return { filled, flagged, unidentified, mappingError: mapping_error };
  }

  // ---------- file uploads (resume and cover letter dropzones) ----------

  function normalize(t) {
    return (t || "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
  }

  function kindOf(text) {
    // Greenhouse's new forms name the field id="cover_letter".
    if (/cover[\s_-]*letter/.test(text)) return "cover_letter";
    if (/resume|curriculum|\bcv\b/.test(text)) return "resume";
    return null;
  }

  function fileKind(el) {
    // nearest signal wins: the input's own attrs first, then close
    // ancestors (dropzones hide the input and put the words on the
    // wrapper). Never read as far up as the whole form, where another
    // upload field's words would bleed in.
    const own = normalize([el.name, el.id, el.accept, labelFor(el)].join(" "));
    let kind = kindOf(own);
    if (kind) return kind;
    let node = el.parentElement;
    for (let i = 0; i < 2 && node && node.tagName !== "FORM"; i++, node = node.parentElement) {
      const t = node.textContent || "";
      if (t.length < 400) {
        kind = kindOf(normalize(t));
        if (kind) return kind;
      }
    }
    return null;
  }

  function visibleHost(el) {
    let node = el;
    while (node && node !== document.body) {
      if (visible(node)) return node;
      node = node.parentElement;
    }
    return null;
  }

  function b64ToFile(b64, filename) {
    const bin = atob(b64);
    const arr = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
    return new File([arr], filename, { type: "application/pdf" });
  }

  function attach(el, doc) {
    const dt = new DataTransfer();
    dt.items.add(b64ToFile(doc.b64, doc.filename));
    el.files = dt.files;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return el.files.length === 1;
  }

  async function attachDocs() {
    // include hidden inputs on purpose: dropzones almost always hide them
    const inputs = [...document.querySelectorAll('input[type="file"]')];
    if (!inputs.length) return { uploaded: 0, fileFlags: 0, docsFor: "" };

    // A document lookup that fails must not take the field fill down with
    // it: the fields are the point, the attachments are a convenience.
    let resp = null;
    try {
      resp = await chrome.runtime.sendMessage({ type: "latest-docs", payload: docsPage() });
    } catch (e) {
      resp = null;
    }
    const docs = resp && resp.ok ? resp.data : null;
    let uploaded = 0, fileFlags = 0;
    for (const el of inputs) {
      const kind = fileKind(el);
      const host = visibleHost(el);
      if (!kind) {
        if (host) mark(host, FLAGGED_STYLE,
                       "Verbatim: attach this file yourself.");
        fileFlags++;
        continue;
      }
      const doc = docs && docs.found ? docs[kind] : null;
      if (!doc) {
        // Cover letters are never attached: the toolkit has no way to know
        // the one it generated last was written for this company. The
        // panel's Cover letter row offers to make one for this job.
        if (host) mark(host, FLAGGED_STYLE, kind === "cover_letter"
          ? "Verbatim: attach your cover letter yourself, so a letter for "
            + "another company can never go out by mistake."
          : "Verbatim: no resume to attach. Check resume_pdf in "
            + "profile.json" + (docs && docs.reason ? ` (${docs.reason})` : "")
            + ".");
        fileFlags++;
        continue;
      }
      try {
        if (!attach(el, doc)) throw new Error("files not set");
        if (kind === "resume") resumeEls.add(el);
        uploaded++;
      } catch (e) {
        if (host) mark(host, FLAGGED_STYLE,
                       "Verbatim: could not attach here, add it yourself.");
        fileFlags++;
      }
    }
    return { uploaded, fileFlags,
             docsFor: (docs && docs.found && docs.source) || "" };
  }

  // ---------- the correction loop ----------

  let sendTimer = null;
  function watchCorrections() {
    for (const t of tracked) {
      t.el.addEventListener("change", () => {
        clearTimeout(sendTimer);
        sendTimer = setTimeout(sendCorrections, 1500);
      });
    }
  }

  function sendCorrections() {
    const now = t => ((t.read ? t.read() : t.el.value) || "").trim();
    const changed = tracked
      .filter(t => now(t) !== (t.filled || "").trim())
      .map(t => ({ signature: t.sig, key: t.key, filled: t.filled,
                   corrected: now(t) }));
    if (!changed.length) return;
    chrome.runtime.sendMessage({
      type: "corrections",
      payload: { domain: location.hostname, fill_id: fillId,
                 corrections: changed },
    });
  }
  window.addEventListener("pagehide", sendCorrections);

  // ---------- saving typed answers into the repo ----------

  function collectAnswers() {
    const factEls = new Set(tracked.map(t => t.el));
    const els = [...document.querySelectorAll("textarea, input[type='text']")]
      .filter(visible)
      .filter(el => !factEls.has(el))
      .filter(el => (el.value || "").trim().length >= 30);
    return els.map(el => {
      // A field Verbatim inserted into carries what it put there, so the
      // save can learn from the edits, and marks it sent so leaving the
      // page does not record it again.
      const a = assisted.get(el);
      if (a) a.sent = el.value.trim();
      return {
        question: labelFor(el).slice(0, 200),
        answer: el.value.trim(),
        inserted: a ? a.inserted : undefined,
        group: a ? a.group : undefined,
        from: a ? a.from : undefined,
        typed: a ? false : typedByHim(el, el.value.trim()),
      };
    }).filter(a => a.question);
  }

  // ---------- suggesting answers from the repo ----------

  // A free-text question, as opposed to a fact field: a textarea, or a
  // text input whose label reads like a question rather than a label.
  function collectQuestions() {
    const factEls = new Set(tracked.map(t => t.el));
    const els = [...document.querySelectorAll("textarea, input[type='text']")]
      .filter(visible)
      .filter(el => !factEls.has(el))
      .filter(el => !(el.value || "").trim())
      .filter(el => el.tagName === "TEXTAREA" ||
                    /\?\s*\*?$/.test(labelFor(el)) || labelFor(el).length > 40);
    return els.map(el => ({ el, question: labelFor(el).slice(0, 200) }))
      .filter(q => q.question);
  }

  // ---------- in-page UI ----------
  //
  // Everything the toolkit shows on a page lives in one shadow root. Not
  // for tidiness: a plain div inherits the site's CSS, and a site that
  // sets line-height on div collapsed the offer card's two lines on top
  // of each other. Inside a shadow root the page cannot reach in, and
  // `all: initial` on the host stops inheritance coming the other way.
  //
  // Since 0.5.0 the main surface is a panel docked to the right edge, laid
  // out after Jobright's filler: white
  // panel, hairline cards, one big mint Autofill button. The page is
  // narrowed while the panel is open, so the panel never sits on the form.
  // On a phone width it is a tab that opens a bottom sheet instead.

  const PANEL_W = 384;
  // Docked only when the form keeps at least 480px beside the panel, so an
  // iPad in portrait gets the tab and sheet rather than a squeezed form.
  const NARROW = PANEL_W + 480;
  const narrow = () => innerWidth < NARROW;

  const UI_CSS = `
    :host {
      all: initial;
      /* The app's departure board look (index.html, html.board): warm grey,
         condensed caps for names and buttons, mono for everything else, ink
         for actions, and the one hot violet only on Autofill and Approve. */
      --ground: #E4E2DE; --card: #EFEDE9; --cell: #F8F6F2;
      --ink: #1A1917; --ink-2: #57544F; --muted: #67635D; --sub: #57544F;
      --soft: #DAD7D1; --line: #CFCCC6; --line-2: #B9B5AE; --rule: #CFCCC6;
      --hover: #D3D0CA;
      --act: #1A1917; --act-hov: #000000; --act-in: #F8F6F2; --strip: #DAD7D1;
      --hot: #6B4EFF; --hot-hov: #5A3DF0; --hot-in: #FFFFFF;
      --att: #A25A0E; --bad: #b42318; --good: #00875a; --look-tint: #E9E2D2;
      --field: #F8F6F2; --field-line: #B9B5AE; --focus: #1A1917;
      --lift: rgba(0,10,20,.16);
      --cond: "Verbatim Cond", "Barlow Condensed", "Arial Narrow", "Helvetica Neue", sans-serif;
      --mono: "Verbatim Mono", "JetBrains Mono", ui-monospace, "SF Mono", Menlo, monospace;
      color-scheme: light;
    }
    /* Dark: the night board */
    :host([data-theme="dark"]) {
      --ground: #0F0E0C; --card: #171614; --cell: #1F1D1A;
      --ink: #E6E1D7; --ink-2: #C4BEB3; --muted: #A39D93; --sub: #D9B877;
      --soft: #23211E; --line: #2E2B27; --line-2: #3D3934; --rule: #2E2B27;
      --hover: #2A2824;
      --act: #E6E1D7; --act-hov: #FFFFFF; --act-in: #0F0E0C; --strip: #2A2824;
      --hot: #7D7CF0; --hot-hov: #8F8EF4; --hot-in: #FFFFFF;
      --att: #D9B877; --bad: #f97066; --good: #3ccf91; --look-tint: #2A2419;
      --field: #171614; --field-line: #3D3934; --focus: #E6E1D7;
      --lift: rgba(0,0,0,.5);
      color-scheme: dark;
    }
    * { box-sizing: border-box; }
    [hidden] { display: none !important; }

    /* ---- the panel ---- */

    #panel {
      position: fixed; top: 0; right: 0; bottom: 0; width: ${PANEL_W}px;
      z-index: 2147483646; display: flex; flex-direction: column;
      background: var(--ground); color: var(--ink);
      font: 400 13px/1.5 var(--mono); -webkit-font-smoothing: antialiased;
      box-shadow: inset 1px 0 var(--line), 0 0 32px rgba(0,10,20,.1);
    }
    #panel.off, #panel.tucked { display: none; }
    .top {
      display: flex; align-items: center; gap: 8px; padding: 12px 12px 12px 16px;
      box-shadow: inset 0 -2px var(--ink); flex: none;
    }
    .brand {
      display: flex; align-items: center; gap: 8px;
      font: 800 26px/1 var(--cond); text-transform: uppercase; letter-spacing: .01em;
    }
    .brand svg { display: none; }
    .lamp {
      width: 7px; height: 7px; border-radius: 50%; background: var(--line-2);
      margin-left: 2px;
    }
    .lamp[data-state="on"] { background: var(--good); }
    .lamp[data-state="working"] { background: var(--att); }
    .top .spacer { flex: 1; }
    .iconbtn {
      width: 32px; height: 32px; border: 0; border-radius: 6px; padding: 0;
      background: none; color: var(--ink); cursor: pointer;
      box-shadow: inset 0 0 0 1px var(--line-2);
      display: flex; align-items: center; justify-content: center;
    }
    .iconbtn:hover { background: var(--soft); }
    .iconbtn.round { width: 32px; height: 32px; }
    .iconbtn svg { width: 16px; height: 16px; }
    .iconbtn.round svg { width: 14px; height: 14px; }
    .iconbtn:focus-visible, button:focus-visible, a:focus-visible {
      outline: 2px solid var(--focus); outline-offset: 2px;
    }

    .body {
      flex: 1; min-height: 0; overflow-y: auto; scrollbar-width: none;
      padding: 12px 12px 20px; display: flex; flex-direction: column; gap: 12px;
    }
    .body::-webkit-scrollbar { display: none; }
    /* Cards keep their height and the body scrolls, rather than the
       column squeezing the clipped list cards shut. */
    .body > * { flex: none; }
    .box {
      background: var(--card); border-radius: 8px; padding: 12px;
      box-shadow: inset 0 0 0 1px var(--line);
    }
    #stack:empty { display: none; }
    #stack { display: flex; flex-direction: column; gap: 12px; }

    /* context: who this is for */
    .ctx-top { display: flex; align-items: center; gap: 12px; }
    .tile {
      width: 40px; height: 40px; flex: none; border-radius: 6px;
      background: var(--cell); color: var(--ink); overflow: hidden;
      display: flex; align-items: center; justify-content: center;
      font: 800 24px/1 var(--cond); text-transform: uppercase;
      box-shadow: inset 0 0 0 1px var(--ink);
    }
    .tile img { width: 100%; height: 100%; object-fit: contain; background: #fff; }
    .who { flex: 1; min-width: 0; }
    .who b { display: block; font: 700 18px/1.05 var(--cond); text-transform: uppercase; letter-spacing: .03em; }
    .who span { display: block; margin-top: 2px; font-size: 11.5px; line-height: 16px; color: var(--ink-2); }
    .pill {
      flex: none; padding: 3px 7px; border-radius: 3px;
      display: flex; align-items: center;
      font: 600 10px/1.3 var(--mono); letter-spacing: .12em; text-transform: uppercase;
      background: none; border: 1px solid var(--line-2); color: var(--ink-2);
    }
    .pill.strong { background: var(--ink); border-color: var(--ink); color: var(--ground); }
    .pill.look { border-color: var(--att); color: var(--att); }
    .pill.stretch { border-style: dashed; color: var(--muted); }
    .role { font: 800 24px/1 var(--cond); text-transform: uppercase; letter-spacing: .005em; margin: 12px 0 6px; }
    .meta { font-size: 11.5px; line-height: 16px; color: var(--ink-2); }
    .meta .dim { color: var(--muted); }

    /* the fill button, and what replaces it once a fill has run */
    .fill-group { display: flex; flex-direction: column; }
    .nextstep { font-size: 12px; margin: 0 2px 8px; color: var(--ink-2); }
    button.autofill {
      position: relative; z-index: 2; width: 100%; height: 48px;
      border: 0; border-radius: 6px;
      background: var(--hot); color: var(--hot-in);
      font: 800 22px/1 var(--cond); text-transform: uppercase; letter-spacing: .05em;
      cursor: pointer; margin-bottom: -24px; overflow: hidden;
    }
    button.autofill:hover:not(:disabled) { background: var(--hot-hov); }
    button.autofill:disabled { cursor: default; }
    .autofill .bar {
      position: absolute; left: 0; right: 0; bottom: 0; height: 4px;
      background: rgba(255,255,255,.2); overflow: hidden;
    }
    .autofill .bar i {
      position: absolute; top: 0; bottom: 0; width: 40%;
      background: rgba(255,255,255,.85); border-radius: 2px;
      animation: slide 1.1s ease-in-out infinite;
    }
    @keyframes slide { from { left: -40%; } to { left: 100%; } }
    .strip {
      background: var(--soft); border-radius: 6px; padding: 32px 8px 8px;
      display: flex; align-items: center; justify-content: center; gap: 6px;
      font: 500 11px/1.4 var(--mono); letter-spacing: -.01em; text-align: center; color: var(--ink);
    }
    .strip svg { width: 14px; height: 14px; flex: none; }
    .res {
      position: relative; z-index: 2; margin-bottom: -24px;
      box-shadow: inset 0 0 0 1px var(--line);
    }
    .res-title { font: 800 22px/1 var(--cond); text-transform: uppercase; letter-spacing: .01em; }
    .res-bar {
      height: 8px; border-radius: 3px; background: var(--soft);
      margin: 10px 0; overflow: hidden;
    }
    .res-bar i { display: block; height: 100%; background: var(--ink); border-radius: 3px; }
    .res-note { font-size: 11.5px; line-height: 16px; color: var(--muted); }
    .res-note.bad { color: var(--bad); }
    .needs { margin-top: 10px; }
    .needs-head { font: 500 10px/1.3 var(--mono); letter-spacing: .14em; text-transform: uppercase; color: var(--muted); margin-bottom: 4px; }
    .need {
      display: flex; align-items: flex-start; gap: 8px; width: 100%;
      padding: 8px 0; border: 0; background: none; cursor: pointer;
      text-align: left; font: 13px/1.45 var(--mono); color: var(--ink);
      border-top: 1px solid var(--rule);
    }
    .need:first-of-type { border-top: 0; }
    .need:hover .need-label { text-decoration: underline; }
    .need .ndot {
      width: 7px; height: 7px; border-radius: 50%; background: var(--att);
      flex: none; margin-top: 6px;
    }
    .need .need-text { flex: 1; min-width: 0; }
    .need .need-why { display: block; font-size: 11.5px; color: var(--muted); }
    .linkbtn {
      border: 0; background: none; padding: 0; cursor: pointer;
      font: 13px/1.45 var(--mono); color: var(--ink);
      text-decoration: underline; text-underline-offset: 2px;
    }
    .res .again { margin-top: 10px; }

    /* the list card: autofill info, resume, cover letter */
    .list {
      border-radius: 8px; overflow: hidden; background: var(--card);
      box-shadow: inset 0 0 0 1px var(--line);
    }
    .sec { padding: 12px; }
    .sec + .sec { border-top: 1px solid var(--rule); }
    .sec-head {
      display: flex; align-items: center; gap: 8px; width: 100%;
      border: 0; padding: 0; background: none; color: var(--ink);
      font: 700 18px/1 var(--cond); text-transform: uppercase; letter-spacing: .03em; text-align: left;
    }
    button.sec-head { cursor: pointer; }
    button.sec-head:hover .sec-label { text-decoration: underline; text-underline-offset: 3px; }
    .sec-head svg.i { width: 16px; height: 16px; flex: none; }
    .sec-head .sec-label { flex: 1; min-width: 0; }
    .sec-head .val { font: 500 11px/1.3 var(--mono); letter-spacing: .08em; color: var(--muted); }
    .sec-head svg.chev { width: 14px; height: 14px; flex: none; }
    .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--bad); flex: none; }
    .sec .line { margin-top: 8px; font-size: 12.5px; line-height: 1.5; }
    .sec .line.dim { color: var(--muted); font-size: 11.5px; }
    .sec .line.att { color: var(--att); font-size: 11.5px; }
    .soft {
      display: flex; align-items: center; justify-content: center; gap: 6px;
      width: 100%; height: 36px; margin-top: 8px; padding: 0 12px;
      border: 0; border-radius: 6px; background: none; color: var(--ink);
      box-shadow: inset 0 0 0 1.5px var(--ink);
      font: 700 15px/1 var(--cond); text-transform: uppercase; letter-spacing: .05em; cursor: pointer;
    }
    .soft:hover:not(:disabled) { background: var(--soft); }
    .soft:disabled { cursor: default; color: var(--muted); box-shadow: inset 0 0 0 1.5px var(--line-2); }
    .soft svg { width: 16px; height: 16px; flex: none; }
    .pair { display: flex; gap: 8px; }
    .pair .soft { flex: 1; }

    /* cover letter review, as on the Tailor page */
    .lr-step { display: flex; align-items: center; gap: 8px; margin-top: 10px; }
    .lr-ver { flex: 1; text-align: center; font: 700 21px/1 var(--cond); text-transform: uppercase; letter-spacing: .02em; white-space: nowrap; }
    .lr-arrow {
      width: 40px; height: 40px; padding: 0 0 3px; flex: none; border: 0; border-radius: 6px; background: none;
      box-shadow: inset 0 0 0 1.5px var(--ink);
      color: var(--ink); font: 400 26px/1 var(--mono); cursor: pointer;
    }
    .lr-arrow:hover:not(:disabled) { background: var(--soft); }
    .lr-arrow:disabled { opacity: .3; cursor: default; }
    .lr-src { overflow-wrap: anywhere; }
    .lr-text, .lr-note {
      display: block; width: 100%; box-sizing: border-box; margin-top: 8px;
      border: 1px solid var(--field-line); border-radius: 6px; background: var(--field); color: var(--ink);
      font: 400 12.5px/1.55 var(--mono); resize: vertical;
    }
    .lr-text {
      height: 260px; padding: 14px 16px; border: 0; background: var(--cell);
      box-shadow: inset 0 0 0 1px var(--line); font-size: 13px; line-height: 1.75;
    }
    .lr-note { height: 104px; padding: 9px 11px; }
    .lr-text:focus, .lr-note:focus { outline: 2px solid var(--ink); outline-offset: -2px; }
    .lr-label { margin-top: 14px; font: 500 10.5px/1.4 var(--mono); letter-spacing: .12em; text-transform: uppercase; color: var(--muted); }
    button.lr-go {
      width: 100%; height: 44px; margin-top: 10px; border: 0; border-radius: 6px;
      background: var(--hot); color: var(--hot-in);
      font: 800 19px/1 var(--cond); text-transform: uppercase; letter-spacing: .05em; cursor: pointer;
    }
    button.lr-go:hover:not(:disabled) { background: var(--hot-hov); }
    button.lr-go:disabled { opacity: .55; cursor: default; }
    button.plainlink {
      margin-top: 10px; border: 0; padding: 0; background: none; color: var(--muted);
      font: 500 11px/1 var(--mono); letter-spacing: .08em; text-transform: uppercase; cursor: pointer;
      text-decoration: underline; text-underline-offset: 3px;
    }
    button.plainlink:hover { color: var(--ink); }

    /* written questions */
    .qitem {
      display: flex; align-items: flex-start; gap: 8px; width: 100%;
      padding: 9px 0; border: 0; border-top: 1px solid var(--rule);
      background: none; cursor: pointer; text-align: left;
      font: 12.5px/1.45 var(--mono); color: var(--ink);
    }
    .qitem:hover .qtext { text-decoration: underline; }
    .qitem .qtext { flex: 1; min-width: 0; }
    .qitem .qstate {
      flex: none; font: 600 10px/1.3 var(--mono); letter-spacing: .12em; text-transform: uppercase;
      color: var(--ink-2); border: 1px solid var(--line-2); border-radius: 3px; padding: 2px 6px;
    }
    .qitem .qstate.done { background: var(--ink); border-color: var(--ink); color: var(--ground); }
    .qlist { margin-top: 8px; }

    /* settings, behind the gear */
    .set-row { display: flex; align-items: center; gap: 10px; font-size: 12.5px; }
    .set-row span { flex: 1; }
    .switch {
      position: relative; width: 34px; height: 20px; flex: none;
      border-radius: 10px; border: 0; padding: 0; cursor: pointer;
      background: var(--line-2);
    }
    .switch[aria-checked="true"] { background: var(--ink); }
    .switch i {
      position: absolute; top: 2px; left: 2px; width: 16px; height: 16px;
      border-radius: 50%; background: var(--cell); transition: left .15s;
      box-shadow: 0 1px 2px rgba(0,0,0,.2);
    }
    .switch[aria-checked="true"] i { left: 16px; }
    .theme-row { margin-top: 12px; }
    .seg { display: flex; gap: 2px; padding: 3px; border-radius: 8px; background: var(--soft); border: 1px solid var(--line); }
    .seg button { border: 0; background: none; color: var(--muted); cursor: pointer;
      font: 600 10.5px/16px var(--mono); letter-spacing: .06em; text-transform: uppercase; padding: 5px 10px; border-radius: 5px; }
    .seg button[aria-pressed="true"] { background: var(--cell); color: var(--ink);
      box-shadow: 0 1px 0 var(--line); }
    .tsteps { display: grid; gap: 4px; margin-top: 10px; }
    .tsteps .tbar { height: 4px; border-radius: 2px; background: var(--line); overflow: hidden; }
    .tsteps .tbar i { display: block; height: 100%; background: var(--hot, #6B4EFF); transition: width .6s ease; }
    .tsteps ol { list-style: none; margin: 0; padding: 0; display: grid; gap: 1px; font: 500 12px/16px var(--mono); }
    .tsteps li { color: var(--muted); }
    .tsteps li.done::before { content: "✓ "; }
    .tsteps li.on { color: var(--ink); font-weight: 700; }

    a.foot {
      align-self: center; margin: 4px 0; padding: 0 4px;
      font: 500 11px/16px var(--mono); letter-spacing: .08em; text-transform: uppercase; color: var(--muted);
      text-decoration: none; cursor: pointer;
    }
    a.foot:hover { color: var(--ink); text-decoration: underline; text-underline-offset: 3px; }

    /* the tab the panel folds into */
    /* Low on the right edge by default: Jobright's own folded tab sits near
       the top, and chat bubbles sit in the bottom corner. Drag it up or
       down to move it; where you leave it is remembered. */
    #tab {
      position: fixed; right: 0; top: auto; bottom: 168px; z-index: 2147483646;
      touch-action: none;
      display: flex; align-items: center;
      padding: 8px 10px 8px 10px; border: 0; cursor: pointer;
      background: var(--card); border-radius: 8px 0 0 8px;
      box-shadow: inset 0 0 0 1px var(--line), 0 0 16px rgba(0,10,20,.12);
      font: 600 13px/16px var(--mono); color: var(--ink);
    }
    #tab svg.logo { width: 34px; height: 34px; display: block; }
    /* What still needs you, as a small count on the logo's corner. */
    #tab .badge {
      position: absolute; top: -5px; left: -5px;
      box-shadow: 0 0 0 2px var(--card);
      min-width: 18px; height: 18px; padding: 0 5px; border-radius: 9px;
      background: var(--att); color: #fff; font: 600 11px/18px var(--mono);
      text-align: center;
    }

    /* ---- cards inside the panel (seen, applied, stored answers) ---- */

    .card {
      background: var(--card); border-radius: 8px; padding: 12px;
      box-shadow: inset 0 0 0 1px var(--line);
      font: 12.5px/1.5 var(--mono); color: var(--ink);
    }
    .card.warn { background: var(--look-tint); box-shadow: inset 3px 0 var(--att), inset 0 0 0 1px var(--line); }
    .warn-line { font-weight: 600; color: var(--att); }
    .head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
    .head .mark { flex: 1; font: 700 18px/1 var(--cond); text-transform: uppercase; letter-spacing: .03em; }
    .x {
      background: none; border: 0; padding: 0 2px; cursor: pointer;
      font: 18px/1 var(--mono); color: var(--muted);
    }
    .x:hover { color: var(--ink); }
    p { margin: 0 0 10px; font-size: 12.5px; line-height: 1.5; }
    .btns { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    button.act, button.ghost {
      font: 700 15px/1 var(--cond); text-transform: uppercase; letter-spacing: .05em;
      height: 36px; padding: 0 14px; border-radius: 6px; border: 0; cursor: pointer;
    }
    button.act { background: var(--ink); color: var(--act-in); }
    button.act:hover:not(:disabled) { background: var(--act-hov); }
    button.act:disabled { opacity: .55; cursor: default; }
    button.ghost { background: none; box-shadow: inset 0 0 0 1.5px var(--ink); color: var(--ink); }
    button.ghost:hover { background: var(--soft); }
    input.text {
      width: 100%; margin-bottom: 10px; padding: 9px 11px;
      border: 1px solid var(--field-line); border-radius: 6px; background: var(--field);
      font: 13px/1.4 var(--mono); color: var(--ink);
    }
    input.text:focus { outline: 2px solid var(--ink); outline-offset: -1px; }
    .q + .q { border-top: 1px solid var(--rule); margin-top: 12px; padding-top: 12px; }
    .q .ask { font-size: 11.5px; color: var(--muted); margin-bottom: 6px; }
    .q .said {
      max-height: 118px; overflow: auto; background: var(--cell);
      box-shadow: inset 0 0 0 1px var(--line);
      border-radius: 6px; padding: 10px; margin-bottom: 8px;
      white-space: pre-wrap; font-size: 12.5px;
    }
    .q .used { font-size: 11px; color: var(--muted); margin-top: 7px; }
    .note { font-size: 11.5px; color: var(--muted); }
    .att { font-size: 11.5px; color: var(--att); }

    /* The answer card sits beside the field it is for, never under the panel. */
    #field {
      position: fixed; z-index: 2147483647; width: min(360px, calc(100vw - 16px));
      max-height: 70vh; overflow: auto;
      box-shadow: 0 8px 28px var(--lift), inset 0 0 0 1px var(--line);
    }
    #pill {
      position: fixed; z-index: 2147483647; max-width: min(280px, calc(100vw - 16px));
      display: flex; align-items: baseline; gap: 7px; text-align: left;
      font: 500 11.5px/16px var(--mono); color: var(--ink); background: var(--card);
      border: 0; border-radius: 6px; padding: 5px 10px 5px 9px; cursor: pointer;
      box-shadow: 0 2px 8px rgba(0,10,20,.10), inset 0 0 0 1px var(--line-2);
    }
    #pill::before {
      content: ""; flex: none; width: 6px; height: 6px; border-radius: 50%;
      background: var(--hot); transform: translateY(-1px);
    }
    #pill:hover:not(:disabled) { background: var(--hover); }
    #pill:disabled { cursor: default; color: var(--muted); }
    #field .label {
      font: 500 10.5px/1.4 var(--mono); color: var(--muted);
      letter-spacing: .12em; text-transform: uppercase; margin-bottom: 6px;
    }
    #field .said {
      max-height: 300px; overflow: auto; background: var(--cell);
      box-shadow: inset 0 0 0 1px var(--line);
      border-radius: 6px; padding: 10px; margin-bottom: 8px;
      white-space: pre-wrap; font-size: 12.5px;
    }
    #field .said.draft { white-space: normal; }
    #field .btns { margin-top: 10px; }
    #field .used { font-size: 11px; color: var(--muted); margin-top: 8px; }
    #field .att { margin-top: 6px; }
    .sent.unsourced {
      color: var(--ink); background: var(--look-tint);
      border-bottom: 1px dashed var(--att); border-radius: 2px;
    }
    .cut {
      background: none; border: 0; padding: 0 3px; margin-left: 2px;
      cursor: pointer; color: var(--att); font: 13px/1 var(--mono);
    }
    .cut:hover { color: var(--ink); }
    .count { font-size: 11px; color: var(--muted); }
    .count.over { color: var(--att); }
    /* Posting checks, story and fact pickers, voice and skim lines */
    #checks:empty { display: none; }
    #checks { display: flex; flex-direction: column; gap: 12px; }
    .quote { font-size: 12px; line-height: 1.5; color: var(--ink);
      border-left: 2px solid var(--line-2); padding: 1px 0 1px 8px; margin: 6px 0 0; }
    .card.warn .quote { border-left-color: var(--att); }
    .why { display: block; font-size: 11px; color: var(--muted); margin-top: 2px; }
    .follow { display: flex; gap: 8px; align-items: flex-start; margin: 8px 0 0;
      font-size: 12px; line-height: 1.5; cursor: pointer; }
    .follow input { margin: 2px 0 0; accent-color: var(--ink); flex: none; }
    .card .btns.sm { margin-top: 10px; }
    .chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
    .chip { font: 600 10.5px/1.3 var(--mono); letter-spacing: .08em; text-transform: uppercase;
      padding: 4px 8px; border-radius: 3px; background: none; border: 0;
      box-shadow: inset 0 0 0 1px var(--line-2); color: var(--ink-2);
      cursor: pointer; text-align: left; }
    .chip[aria-pressed="true"] { background: var(--ink); box-shadow: none; color: var(--ground); }
    #field .pick { margin-top: 10px; }
    #field .pick .label { margin-bottom: 4px; }
    #field select.story { width: 100%; font: 12.5px/18px var(--mono); padding: 6px 8px;
      border: 1px solid var(--field-line); border-radius: 6px; background: var(--field); color: var(--ink); }
    #field .linkbtn { margin-top: 6px; }
    /* Versions and the revise note, one row each, kept small. */
    #field .vrow { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 6px; }
    #field .vrow .label { margin-bottom: 0; }
    #field .vnav { display: flex; align-items: center; gap: 2px; font: 600 11px/14px var(--mono); color: var(--muted); }
    #field .vnav .linkbtn { margin: 0; padding: 0; width: 24px; height: 24px; display: inline-flex;
      align-items: center; justify-content: center; font-size: 16px; line-height: 1; }
    #field .vnav .linkbtn:disabled { opacity: .35; cursor: default; }
    #field .saidwrap { position: relative; }
    #field .saidwrap.more::after { content: ""; position: absolute; left: 1px; right: 1px; bottom: 9px;
      height: 16px; border-radius: 0 0 6px 6px; pointer-events: none;
      background: linear-gradient(to bottom, transparent, var(--cell)); }
    #field .rrow { display: flex; gap: 8px; margin-top: 8px; align-items: center; }
    #field .rrow .text { margin: 0; flex: 1; min-width: 0; }
    .vc { margin-top: 10px; padding-top: 8px; border-top: 1px solid var(--rule);
      font-size: 11.5px; line-height: 1.5; }
    .vc:empty { display: none; }
    .vc .first { color: var(--ink); }
    .vc .ok { color: var(--muted); }
    .vc .bad { color: var(--att); }
    .vc ul { margin: 6px 0 0; padding: 0; list-style: none; color: var(--muted); }
    .vc li { margin-top: 3px; }
    .vc q { quotes: none; color: var(--ink); background: var(--look-tint); border-radius: 2px; padding: 0 2px; }

    /* ---- phone width: a bottom sheet, folded to a tab by default ---- */
    @media (max-width: ${NARROW - 1}px) {
      #panel {
        top: auto; left: 0; width: 100%; height: min(78vh, 640px);
        border-radius: 12px 12px 0 0;
        box-shadow: 0 -8px 32px var(--lift);
      }
      #panel .top { border-radius: 12px 12px 0 0; }
      #panel .fold svg { transform: rotate(90deg); }
      #tab {
        top: auto !important; bottom: 16px !important; right: 16px; border-radius: 22px;
        padding: 6px;
      }
      #field { width: calc(100vw - 16px); }
      /* 16px or iPhone zooms in when a field is tapped */
      .lr-text, .lr-note, input.text, #field select.story { font-size: 16px; }
    }
    @media (prefers-reduced-motion: reduce) {
      .autofill .bar i { animation: none; left: 30%; }
      .switch i { transition: none; }
    }
  `;

  // The extension's own mark (icons/icon.svg), inline so it needs no
  // web-accessible resource and no fetch from the page.
  const LOGO = `<svg class="logo" viewBox="0 0 128 128" aria-hidden="true"><circle cx="64" cy="64" r="60" fill="#6B4EFF"/><g fill="#fff"><circle cx="47" cy="56" r="13"/><path d="M60 56c0 17-8 29-22 34l-4-7c8-4 12-10 13-18z"/><circle cx="81" cy="56" r="13"/><path d="M94 56c0 17-8 29-22 34l-4-7c8-4 12-10 13-18z"/></g></svg>`;
  const ICONS = {
    gear: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>',
    right: '<svg class="chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6"/></svg>',
    left: '<svg class="chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M15 6l-6 6 6 6"/></svg>',
    user: '<svg class="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="3" width="16" height="18" rx="3"/><path d="M4 8h16"/></svg>',
    file: '<svg class="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M8 13h8M8 17h5"/></svg>',
    mail: '<svg class="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 7l9 6 9-6"/></svg>',
    chat: '<svg class="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H8l-4 4V5a2 2 0 0 1 2-2h13a2 2 0 0 1 2 2z"/></svg>',
    spark: '<svg viewBox="0 0 24 24"><path d="M12 2.5c.6 4.6 2.9 6.9 7.5 7.5-4.6.6-6.9 2.9-7.5 7.5-.6-4.6-2.9-6.9-7.5-7.5 4.6-.6 6.9-2.9 7.5-7.5z" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/><path d="M19 15.5c.25 1.9 1.1 2.75 3 3-1.9.25-2.75 1.1-3 3-.25-1.9-1.1-2.75-3-3 1.9-.25 2.75-1.1 3-3z" fill="currentColor"/></svg>',
    shield: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l7 3v6c0 4.5-3 7.7-7 9-4-1.3-7-4.5-7-9V6z"/><path d="M9 12l2 2 4-4"/></svg>',
  };

  let shadow = null;

  // After the extension updates itself, this copy in an open page loses
  // its connection. Say so once instead of failing quietly.
  // Only when it started connected: the test harness has no extension id.
  const startedConnected = !!(chrome.runtime && chrome.runtime.id);
  const updateWatch = setInterval(() => {
    if (!startedConnected) { clearInterval(updateWatch); return; }
    if (chrome.runtime && chrome.runtime.id) return;
    clearInterval(updateWatch);
    if (!shadow) return;
    const note = document.createElement("div");
    note.textContent = "Verbatim was updated. Reload this page to use the new version.";
    note.setAttribute("role", "status");
    note.style.cssText = "position:fixed;right:16px;bottom:16px;z-index:2147483647;"
      + "max-width:300px;padding:10px 14px;border-radius:8px;background:var(--card);"
      + "color:var(--ink);font:12.5px/18px var(--mono);"
      + "box-shadow:0 0 16px var(--lift),inset 0 0 0 1px var(--line);";
    shadow.append(note);
  }, 20000);

  // The app's two faces, bundled in extension/fonts. Read as bytes and
  // added to the page's font set under names of our own, so a page's font
  // policy cannot block them and they never clash with the page's fonts.
  // Shadow roots draw from the document's fonts. Until they load, or if
  // they cannot, the stacks fall back to system condensed and mono.
  const FONTS = [
    ["Verbatim Cond", "fonts/barlow-condensed-600.woff2", "600"],
    ["Verbatim Cond", "fonts/barlow-condensed-700.woff2", "700"],
    ["Verbatim Cond", "fonts/barlow-condensed-800.woff2", "800"],
    ["Verbatim Mono", "fonts/jetbrains-mono.woff2", "100 800"],
  ];
  let fontsAdded = false;
  function addFonts() {
    if (fontsAdded || !chrome.runtime || !chrome.runtime.getURL || !window.FontFace) return;
    fontsAdded = true;
    for (const [family, path, weight] of FONTS) {
      fetch(chrome.runtime.getURL(path))
        .then(r => r.arrayBuffer())
        .then(buf => new FontFace(family, buf, { weight }).load())
        .then(face => document.fonts.add(face))
        .catch(() => {});
    }
  }

  function ui() {
    if (shadow && shadow.host.isConnected) return shadow;
    addFonts();
    const host = document.createElement("div");
    host.id = "job-toolkit-ui";
    shadow = host.attachShadow({ mode: "open" });
    const style = document.createElement("style");
    style.textContent = UI_CSS;
    shadow.append(style);
    // documentElement, not body: some ATS pages replace body wholesale
    document.documentElement.appendChild(host);
    applyTheme(P.theme || "system");
    buildPanel();
    return shadow;
  }

  // Light, dark or follow the system, the same setting as the Verbatim
  // page: the server holds it, this browser keeps a copy so the panel
  // opens in the right colors before the server answers.
  const darkQuery = matchMedia("(prefers-color-scheme: dark)");
  function applyTheme(pref) {
    P.theme = ["system", "light", "dark"].includes(pref) ? pref : "system";
    if (!shadow) return;
    const dark = P.theme === "dark" || (P.theme === "system" && darkQuery.matches);
    shadow.host.dataset.theme = dark ? "dark" : "light";
  }
  darkQuery.addEventListener("change", () => applyTheme(P.theme));
  async function loadTheme() {
    applyTheme(await getPref("theme", "system"));
    try {
      const d = await askServer("ui-theme", {});
      applyTheme(d.theme);
      setPref("theme", P.theme);
    } catch (e) { /* the copy stands */ }
  }

  function panel(name) {
    return ui().getElementById(name);
  }

  // A card inside the panel. Showing one opens the panel, since a card
  // nobody can see asks for nothing.
  function card(name, slot = "stack") {
    panel(name)?.remove();
    const el = document.createElement("div");
    el.className = "card";
    el.id = name;
    ui().getElementById(slot).prepend(el);
    showPanel({ expand: true });
    return el;
  }

  function head(title, onClose) {
    const bar = document.createElement("div");
    bar.className = "head";
    bar.append(Object.assign(document.createElement("span"),
                             { className: "mark", textContent: title }));
    if (onClose) {
      const x = document.createElement("button");
      x.className = "x";
      x.textContent = "×";
      x.title = "Close";
      x.onclick = onClose;
      bar.append(x);
    }
    return bar;
  }

  function button(label, kind = "act") {
    const b = document.createElement("button");
    b.className = kind;
    b.textContent = label;
    return b;
  }

  function showSuggestions(items) {
    // Under the written questions in the panel, where you asked for it.
    const box = card("answers", ui().getElementById("qslot") ? "qslot" : "stack");
    box.append(head("Your answer repo", () => box.remove()));

    let offered = 0;
    for (const it of items) {
      if (!it.answer && !it.flag) continue;
      const row = document.createElement("div");
      row.className = "q";
      row.append(Object.assign(document.createElement("div"),
                               { className: "ask", textContent: it.question }));
      if (it.answer) {
        offered++;
        row.append(Object.assign(document.createElement("div"),
                                 { className: "said", textContent: it.answer }));
        const insert = button("Insert");
        insert.onclick = () => {
          // The user's own words, verbatim. Editing them in the field is the
          // point: what you leave behind is what Save my answers records.
          setNativeValue(it.el, it.answer);
          assisted.set(it.el, { question: it.question, group: it.group || "",
                                inserted: it.answer, from: "best", sent: "" });
          it.el.scrollIntoView({ block: "center", behavior: "smooth" });
          it.el.focus();
          insert.textContent = "Inserted";
          insert.disabled = true;
        };
        row.append(insert);
        if (it.used && it.used.length) {
          row.append(Object.assign(document.createElement("div"), {
            className: "used",
            textContent: "Used on " + it.used.join("; "),
          }));
        }
      }
      if (it.flag) {
        row.append(Object.assign(document.createElement("div"),
                                 { className: "att", textContent: it.flag }));
      }
      box.append(row);
    }
    if (!offered) {
      box.append(Object.assign(document.createElement("div"), {
        className: "note",
        textContent: "Nothing stored for these questions yet. Answer them "
          + "once and Save my answers keeps them for next time.",
      }));
    }
    return offered;
  }

  async function runSuggest() {
    const questions = collectQuestions();
    if (!questions.length) {
      return { error: "no empty free-text questions on this page" };
    }
    const resp = await chrome.runtime.sendMessage({
      type: "answers-suggest",
      payload: { questions: questions.map(q => q.question) },
    });
    if (!resp || !resp.ok) return { error: resp ? resp.error : "no response" };
    const items = (resp.data.suggestions || []).map((s, i) => ({
      ...s, el: questions[i] && questions[i].el,
    })).filter(s => s.el);
    const offered = showSuggestions(items);
    return { asked: questions.length, offered };
  }

  // ---------- posting checks ----------
  //
  // Before anything is drafted the panel reads the posting and the form
  // for three things: a request not to use AI, visible instructions for
  // applicants, and text that is hidden from people or addressed to an AI.
  // Hidden and AI-addressed text is never followed: it is shown to the user as
  // a possible trap and stripped from the posting before it reaches the
  // model. The checks themselves are deterministic, on the server
  // (postcheck.py), so the panel and every draft route agree.

  const SKIP_TEXT = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "SELECT",
                             "OPTION", "TEXTAREA", "SVG", "TITLE"]);

  function rgba(c) {
    const m = (c || "").match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    const p = m[1].split(/[\s,\/]+/).filter(Boolean).map(Number);
    return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
  }

  function lum(c) {
    const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
    return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
  }

  function bgOf(el) {
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
      const cs = getComputedStyle(n);
      if (cs.backgroundImage && cs.backgroundImage !== "none") return null;
      const c = rgba(cs.backgroundColor);
      if (c && c.a > 0.5) return c;
    }
    return { r: 255, g: 255, b: 255, a: 1 };
  }

  // Why an element's own text is hidden from a person, or "".
  const hiddenMemo = new Map();
  function hiddenWhy(el) {
    if (hiddenMemo.has(el)) return hiddenMemo.get(el);
    let why = "";
    for (let n = el; n && n !== document.body && !why; n = n.parentElement) {
      const cs = getComputedStyle(n);
      if (cs.display === "none") why = "display none";
      else if (cs.visibility === "hidden" || cs.visibility === "collapse") why = "visibility hidden";
      else if (parseFloat(cs.opacity) === 0) why = "opacity zero";
      else if (n.getAttribute("aria-hidden") === "true") why = "aria hidden";
      else if (n.hasAttribute("hidden")) why = "hidden attribute";
    }
    if (!why) {
      const cs = getComputedStyle(el);
      const r = el.getBoundingClientRect();
      const docW = document.documentElement.scrollWidth;
      if (parseFloat(cs.fontSize) < 2) why = "zero size text";
      else if (/rect\(\s*0(px)?[\s,]+0(px)?[\s,]+0(px)?[\s,]+0(px)?\s*\)/.test(cs.clip)
               || (r.width <= 2 && r.height <= 2)) why = "zero size";
      else if (r.right + scrollX <= 0 || r.bottom + scrollY <= 0
               || r.left + scrollX >= docW + 50
               || parseFloat(cs.textIndent) < -999) why = "off screen";
      else {
        const fg = rgba(cs.color);
        const bg = bgOf(el);
        if (fg && fg.a < 0.1) why = "transparent text";
        else if (fg && bg) {
          const a = lum(fg), b = lum(bg);
          if ((Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05) < 1.25) {
            why = "same color as the background";
          }
        }
      }
    }
    hiddenMemo.set(el, why);
    return why;
  }

  // Text on the page a person cannot see, and HTML comments. Each piece
  // once, short, with the reason it is hidden.
  function findHidden() {
    hiddenMemo.clear();
    const out = [], seen = new Set();
    const add = (text, why) => {
      text = text.replace(/\s+/g, " ").trim();
      if (text.length < 4 || seen.has(text) || out.length >= 200) return;
      seen.add(text);
      out.push({ text: text.slice(0, 300), why });
    };
    if (!document.body) return out;
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    const byParent = new Map();
    for (let t = walker.nextNode(); t; t = walker.nextNode()) {
      const el = t.parentElement;
      if (!el || SKIP_TEXT.has(el.tagName.toUpperCase()) || el.closest("svg")) continue;
      if (el.id === "job-toolkit-ui") continue;
      const s = t.nodeValue;
      if (!s || !s.trim()) continue;
      byParent.set(el, (byParent.get(el) || "") + " " + s);
    }
    for (const [el, text] of byParent) {
      if (text.trim().length < 4) continue;
      const why = hiddenWhy(el);
      if (why) add(text, why);
    }
    const cw = document.createTreeWalker(document.body, NodeFilter.SHOW_COMMENT);
    for (let c = cw.nextNode(); c; c = cw.nextNode()) {
      const s = (c.nodeValue || "").trim();
      if (s.length >= 12 && !/^\[if |^\s*\/?ko\b/i.test(s)) add(s, "HTML comment");
    }
    return out;
  }

  // The form's own words: labels, help text, instructions. Never values.
  function formText() {
    const forms = [...document.querySelectorAll("form")];
    const roots = forms.length ? forms : [document.body];
    const parts = [];
    for (const f of roots) {
      const walker = document.createTreeWalker(f, NodeFilter.SHOW_TEXT);
      for (let t = walker.nextNode(); t; t = walker.nextNode()) {
        const el = t.parentElement;
        if (!el || SKIP_TEXT.has(el.tagName.toUpperCase())) continue;
        const s = (t.nodeValue || "").trim();
        if (s) parts.push(s);
      }
    }
    return parts.join("\n").slice(0, 8000);
  }

  let checksRun = null;
  function postingCheck() {
    if (checksRun) return checksRun;
    checksRun = (async () => {
      P.hidden = findHidden();
      P.formText = formText();
      try {
        P.checks = await askServer("posting-check", {
          page_text: capturePosting().text, form_text: P.formText,
          hidden: P.hidden, url: location.href,
        });
      } catch (e) {
        P.checks = null;
      }
      if (shadow) renderChecks();
      return P.checks;
    })();
    return checksRun;
  }

  function noAI() {
    return !!(P.checks && P.checks.no_ai && P.checks.no_ai.length);
  }

  function draftingOff() {
    return noAI() && !P.allowAI;
  }

  function quote(text) {
    return h("div", "quote", `"${text}"`);
  }

  function renderChecks() {
    const box = shadow && shadow.getElementById("checks");
    if (!box) return;
    box.replaceChildren();
    const c = P.checks;
    if (!c) return;
    if (c.no_ai && c.no_ai.length) {
      const k = h("div", "card warn");
      k.id = "noai";
      k.append(h("div", "warn-line", "This posting asks for no AI"));
      c.no_ai.slice(0, 2).forEach(n => k.append(quote(n.quote)));
      const btns = h("div", "btns sm");
      if (P.allowAI) {
        k.append(h("div", "note", "Drafting is on for this page because you chose Draft anyway."));
        const off = button("Turn drafting off", "ghost");
        off.onclick = () => { P.allowAI = false; renderChecks(); if (fieldEl) renderField(fieldEl); };
        btns.append(off);
      } else {
        k.append(h("div", "note", "Drafting and reading ahead are off on this page. Your saved answers and facts still fill."));
        const anyway = button("Draft anyway", "ghost");
        anyway.onclick = () => {
          P.allowAI = true;
          renderChecks();
          if (fieldEl) renderField(fieldEl);
          readAhead();
        };
        btns.append(anyway);
      }
      k.append(btns);
      box.append(k);
    }
    if (c.instructions && c.instructions.length) {
      const k = h("div", "card");
      k.id = "instructions";
      k.append(h("div", "head", ""));
      k.firstChild.append(h("span", "mark", "Instructions in the posting"));
      k.append(h("div", "note", "Followed in drafts only once you tick them."));
      for (const ins of c.instructions.slice(0, 4)) {
        const row = h("label", "follow");
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.checked = P.followed.has(ins.quote);
        cb.onchange = () => {
          if (cb.checked) P.followed.add(ins.quote); else P.followed.delete(ins.quote);
        };
        row.append(cb, h("span", "", `"${ins.quote}"`));
        k.append(row);
      }
      box.append(k);
    }
    if (c.traps && c.traps.length) {
      const k = h("div", "card warn");
      k.id = "traps";
      k.append(h("div", "warn-line", c.traps.length > 1
        ? `${c.traps.length} possible traps` : "Possible trap"));
      k.append(h("div", "note", "Hidden or written for an AI. Never followed, and never sent to the model as an instruction."));
      for (const t of c.traps.slice(0, 5)) {
        if (!t) continue;
        const q = quote(t.quote.length > 160 ? t.quote.slice(0, 157) + "..." : t.quote);
        q.append(h("span", "why", t.where === "hidden" ? `Hidden: ${t.why}` : "Written for an AI"));
        k.append(q);
      }
      if (c.traps.length > 5) k.append(h("div", "note", `${c.traps.length - 5} more like these.`));
      box.append(k);
    }
  }

  // ---------- reading questions ahead ----------
  //
  // With the panel open on a form, its written questions are drafted in
  // the background, one model call at a time on the server, so a draft is
  // waiting when the user clicks into each field. Nothing goes into a field
  // until the user presses Insert. Off on a page that asks for no AI.

  let readTimer = null;
  async function readAhead() {
    await postingCheck();
    if (draftingOff()) { P.predraft = "off"; return; }
    const els = questionFields().filter(el => !(el.value || "").trim());
    if (!els.length) { P.predraft = "none"; return; }
    const qs = els.map(el => {
      const st = stateOf(el);
      return { question: st.question, kind: st.kind, limit: st.limit };
    });
    // Replica test pages record the decision without spending model calls.
    if (document.querySelector('meta[name="job-toolkit-autotest"]')
        && !/[?&]noauto\b/.test(location.search)) {
      P.predraft = "on";
      return;
    }
    try {
      await askServer("answers-predraft", {
        questions: qs, url: location.href, title: document.title,
        company: guessCompany(), page_text: capturePosting().text,
        form_text: P.formText, hidden: P.hidden, allow_ai: P.allowAI,
      });
      P.predraft = "on";
    } catch (e) {
      P.predraft = "failed";
      return;
    }
    pollReadAhead(els, Date.now());
  }

  function pollReadAhead(els, started) {
    clearTimeout(readTimer);
    readTimer = setTimeout(async () => {
      let d;
      try {
        d = await askServer("answers-predraft-status", {
          url: location.href, questions: els.map(el => stateOf(el).question),
        });
      } catch (e) {
        return;
      }
      let waiting = 0;
      for (const el of els) {
        const st = stateOf(el);
        const got = (d.drafts || {})[st.question];
        if (!got) continue;
        if (got.state === "ready" && got.result && !st.ready) {
          st.ready = got.result;
          if (fieldEl === el && !st.draft && !st.ask && !st.busy) {
            useReady(el, st);
            renderField(el);
          }
        } else if (got.state === "waiting" || got.state === "drafting") {
          waiting++;
        }
      }
      if (P.open) renderQuestions();
      if (waiting && Date.now() - started < 20 * 60 * 1000) pollReadAhead(els, started);
    }, 8000);
  }

  // A draft made ahead goes up in the card the way a clicked one would,
  // when there is no saved best answer to show first.
  function useReady(el, st) {
    if (!st.ready || draftingOff()) return false;
    const g = st.info && st.info.group;
    if (g && g.best) return false;
    if (st.ready.ask) st.ask = st.ready.ask;
    else st.draft = st.ready;
    st.fromReady = true;
    return true;
  }

  // ---------- voice and skim, as the user edits ----------

  function checkText(el, st) {
    if (st.draft && !st.inserted) return joinSentences(st.draft.sentences || []);
    return el.value || "";
  }

  let vcTimer = null;
  function scheduleCheck(el, ms = 450) {
    clearTimeout(vcTimer);
    vcTimer = setTimeout(() => runCheck(el), ms);
  }

  async function runCheck(el) {
    const st = stateOf(el);
    const text = checkText(el, st);
    if (!text.trim()) { st.check = null; st.checkKey = ""; paintCheck(el); return; }
    if (st.checkKey === text) return;
    st.checkKey = text;
    let r;
    try {
      r = await askServer("answers-check", {
        text, question: st.question, company: guessCompany(),
      });
    } catch (e) {
      return;
    }
    if (st.checkKey !== text) return;
    st.check = r;
    paintCheck(el);
  }

  function paintCheck(el) {
    if (fieldEl !== el || !shadow) return;
    const box = shadow.getElementById("vc");
    if (!box) return;
    box.replaceChildren();
    const c = stateOf(el).check;
    if (!c) return;
    const s = c.skim || {};
    if (s.first) {
      box.append(h("div", "label", "What a skimmer takes away"));
      box.append(h("div", "first", `"${s.line || s.first}"`));
      const keeps = (s.keeps || []).length ? ` Keeps: ${s.keeps.join(", ")}.` : "";
      box.append(s.answers
        ? h("div", "ok", "The first line answers the question." + keeps)
        : h("div", "bad", `The first line does not answer. ${s.why || ""}` + keeps));
    }
    const hints = c.hints || [];
    if (hints.length) {
      const ul = h("ul");
      for (const x of hints.slice(0, 4)) {
        const li = h("li");
        const q = document.createElement("q");
        const t = x.quote.replace(/\s+/g, " ");
        q.textContent = t.length > 48 ? t.slice(0, 45) + "..." : t;
        li.append(q, " ", x.message);
        ul.append(li);
      }
      if (hints.length > 4) ul.append(h("li", "", `${hints.length - 4} more`));
      box.append(ul);
    }
    requestAnimationFrame(placeField);
  }

  document.addEventListener("input", e => {
    if (fieldEl && e.target === fieldEl) scheduleCheck(fieldEl);
  }, true);

  // ---------- story and fact pickers ----------

  let storiesP = null;
  function loadStories() {
    if (!storiesP) {
      storiesP = askServer("answers-stories", {}).then(d => d.stories || [],
                                                       () => []);
    }
    return storiesP;
  }

  function pickers(el, st, box) {
    if (draftingOff()) return;
    const wrap = h("div");
    loadStories().then(stories => {
      if (!stories.length) return;
      const pick = h("div", "pick");
      pick.append(h("div", "label", "Build on a story"));
      const sel = document.createElement("select");
      sel.className = "story";
      sel.setAttribute("aria-label", "Build on a story");
      sel.append(new Option("No story, let the draft choose", ""));
      for (const s of stories) sel.append(new Option(s.title, s.id));
      sel.value = st.story || "";
      sel.onchange = () => { st.story = sel.value; };
      pick.append(sel);
      wrap.prepend(pick);
      requestAnimationFrame(placeField);
    });
    if (st.info && st.info.wants_facts) {
      const pick = h("div", "pick");
      pick.append(h("div", "label", "Company facts to cite"));
      if (st.facts === undefined) {
        const get = h("button", "linkbtn", "Pick from the posting");
        get.onclick = async () => {
          get.disabled = true;
          get.textContent = "Reading the posting...";
          try {
            const d = await askServer("answers-facts", {
              url: location.href, title: document.title, company: guessCompany(),
              page_text: capturePosting().text, hidden: P.hidden,
              form_text: P.formText,
            });
            st.factList = d.facts || [];
            st.facts = new Set();
          } catch (e) {
            get.disabled = false;
            get.textContent = "Could not read it. Try again";
            return;
          }
          if (fieldEl === el) renderField(el);
        };
        pick.append(get);
      } else if (!st.factList.length) {
        pick.append(h("div", "note", "Nothing specific enough in this posting."));
      } else {
        const chips = h("div", "chips");
        for (const f of st.factList) {
          const chip = h("button", "chip", f.fact);
          chip.title = `From the posting: "${f.quote}"`;
          chip.setAttribute("aria-pressed", String(st.facts.has(f.fact)));
          chip.onclick = () => {
            if (st.facts.has(f.fact)) st.facts.delete(f.fact); else st.facts.add(f.fact);
            chip.setAttribute("aria-pressed", String(st.facts.has(f.fact)));
          };
          chips.append(chip);
        }
        pick.append(chips);
      }
      wrap.append(pick);
    }
    box.append(wrap);
  }

  // ---------- the answer card, at the field ----------
  //
  // Click into a written question and a small card sits beside it with
  // your best stored answer, or a draft on request. Nothing goes into the
  // field until you press Insert. Whatever you then send in that field is
  // kept as a new version of the answer, with lessons from your edits.

  // Mirrors answers.py:classify_guarded and _GUARDED. These never get a card.
  const GUARDED_Q = new RegExp(
    "sponsor|visa|work authori|authorized to work|immigration|\\brace\\b|"
    + "ethnicit|gender|disabilit|veteran|salary|compensation|desired pay|"
    + "pay expectation", "i");
  // Mirrors answers.py:_FACT_LABEL, for short labels only, as there.
  const FACT_Q = new RegExp(
    "\\b(linked\\s*in|github|portfolio|website|web\\s*site|url|link|phone|"
    + "mobile|e-?mail|address|first name|last name|full name|zip|postal)\\b",
    "i");
  // Mirrors answers.py:_COVER_NOTE_Q.
  const COVER_NOTE_Q = new RegExp(
    "cover\\s*(letter|note)|anything else|additional (information|info|"
    + "comments)|something else you('d| would) like|else (you'd|you would) "
    + "like (us|to share)", "i");

  function isQuestionField(el) {
    if (!el || !el.isConnected || el.readOnly || el.disabled) return false;
    const textInput = el.tagName === "INPUT" &&
      (el.type === "text" || !el.getAttribute("type"));
    if (el.tagName !== "TEXTAREA" && !textInput) return false;
    // The text box inside a dropdown (react-select types to filter) takes
    // a pick from its list, never a written answer.
    if (el.hasAttribute("aria-autocomplete")
        || el.closest("[role='combobox'], [aria-haspopup='listbox']")) return false;
    if (tracked.some(t => t.el === el)) return false;
    const label = labelFor(el);
    if (!label || GUARDED_Q.test(label)) return false;
    if (label.split(/\s+/).length <= 5 && FACT_Q.test(label)) return false;
    return el.tagName === "TEXTAREA" || /\?\s*\*?$/.test(label) ||
      label.length > 40 || el.maxLength >= 200;
  }

  // Helper text near a field, where forms put "500 characters max".
  function hintFor(el) {
    const ids = (el.getAttribute("aria-describedby") || "").split(/\s+/);
    const parts = ids.map(id => id && document.getElementById(id))
      .filter(Boolean).map(n => n.textContent);
    const host = el.parentElement;
    if (host && host.textContent.length < 400) parts.push(host.textContent);
    return parts.join(" ");
  }

  function limitOf(el, label) {
    const out = {};
    if (el.maxLength > 0 && el.maxLength < 100000) out.chars = el.maxLength;
    const near = `${label} ${hintFor(el)}`;
    const num = s => parseInt(s.replace(/,/g, ""), 10);
    let m = near.match(/(\d[\d,]*)\s*(?:characters?|chars?)\b/i);
    if (m && !out.chars) out.chars = num(m[1]);
    m = near.match(/(\d[\d,]*)\s*words?\b/i);
    if (m) out.words = num(m[1]);
    return out;
  }

  function limitWords(limit) {
    return [limit.chars && `${limit.chars} characters`,
            limit.words && `${limit.words} words`].filter(Boolean).join(", ");
  }

  function overLimit(text, limit) {
    if (limit.chars && text.length > limit.chars) return true;
    const words = text.trim() ? text.trim().split(/\s+/).length : 0;
    return !!(limit.words && words > limit.words);
  }

  const fieldState = new WeakMap();
  // Fields Verbatim inserted into, and what it put there, for learning.
  const assisted = new Map();
  let fieldEl = null;
  let choiceEl = null; // the dropdown or option set its pill is for
  const closedFields = new WeakSet();

  function stateOf(el) {
    if (!fieldState.has(el)) {
      const question = labelFor(el).slice(0, 300);
      fieldState.set(el, {
        question, limit: limitOf(el, question),
        kind: COVER_NOTE_Q.test(question) ? "cover note" : "answer",
        info: null, draft: null, ask: "", busy: "", error: "",
      });
    }
    return fieldState.get(el);
  }

  function fieldBox() {
    const root = ui();
    let box = root.getElementById("field");
    if (!box) {
      box = document.createElement("div");
      box.id = "field";
      box.className = "card";
      root.append(box);
    }
    return box;
  }

  // The quiet pill beside a field. The card opens only when it is clicked.
  function pillBox() {
    const root = ui();
    let p = root.getElementById("pill");
    if (!p) {
      p = document.createElement("button");
      p.id = "pill";
      root.append(p);
    }
    p.disabled = false;
    p.title = "";
    return p;
  }

  function closeField() {
    if (recording) stopSaying(true);
    ui().getElementById("field")?.remove();
    if (!choiceEl) ui().getElementById("pill")?.remove();
    ui().getElementById("panel").classList.remove("tucked");
    fieldEl = null;
  }

  // Off to the side of the field: right when there is room, else left.
  // Only when neither side has room, below the field and any option list
  // it has open. Never above it, never on top of it or its options. Never
  // under the panel either: while it is docked the page ends where the
  // panel starts, and on a phone, where the sheet would sit on top, the
  // sheet tucks away until this closes. Fixed, so it is recomputed on
  // scroll.
  function overlaps(a, b, pad = 8) {
    return a.left < b.right + pad && a.right > b.left - pad &&
      a.top < b.bottom + pad && a.bottom > b.top - pad;
  }

  // The option list a dropdown has open right now, if any.
  function openList(el) {
    const own = [el, el.querySelector && el.querySelector("input")].filter(Boolean);
    const ids = own.flatMap(n => [n.getAttribute("aria-controls"), n.getAttribute("aria-owns")])
      .filter(Boolean).join(" ").split(/\s+/).filter(Boolean);
    for (const id of ids) {
      const l = document.getElementById(id);
      if (l && l.getClientRects().length) return l;
    }
    let up = el.parentElement;
    for (let i = 0; up && i < 3; i++, up = up.parentElement) {
      const l = [...up.querySelectorAll("[role='listbox']")]
        .find(n => !n.contains(el) && n.getClientRects().length);
      if (l) return l;
    }
    return null;
  }

  function placeNear(box, el, fit) {
    const pnl = shadow.getElementById("panel");
    const tab = shadow.getElementById("tab");
    pnl.classList.remove("tucked");
    const r = el.getBoundingClientRect();
    const list = openList(el);
    const lr = list && list.getBoundingClientRect();
    const u = lr ? { left: Math.min(r.left, lr.left), right: Math.max(r.right, lr.right),
                     top: Math.min(r.top, lr.top), bottom: Math.max(r.bottom, lr.bottom) } : r;
    const docked = P.open && !P.collapsed && !narrow();
    const right = docked ? innerWidth - PANEL_W : innerWidth;
    const gap = 10;
    // Narrower than its usual 360 when that is what fits beside the field.
    const room = right - u.right - gap - 8;
    if (fit) box.style.width = !narrow() && room >= 300 && room < 360 ? `${room}px` : "";
    box.style.maxHeight = "";
    const w = box.offsetWidth, h = box.offsetHeight;
    // Level with the field's first line, so a pill sits beside the box.
    const y = Math.min(Math.max(8, r.top + Math.max(0, (Math.min(r.height, 44) - h) / 2)),
                       innerHeight - h - 8);
    const spots = [];
    if (room >= w) spots.push([u.right + gap, y]);
    if (u.left - gap - w >= 8) spots.push([u.left - gap - w, y]);
    const below = [Math.min(Math.max(8, u.left), right - w - 8), u.bottom + gap];
    spots.push(below);
    const avoid = [];
    if (P.open && !P.collapsed) avoid.push(pnl.getBoundingClientRect());
    if (P.open && P.collapsed && !tab.hidden) avoid.push(tab.getBoundingClientRect());
    const rect = ([x, y]) => ({ left: x, top: y, right: x + w, bottom: y + h });
    let spot = spots.find(p => !avoid.some(a => overlaps(rect(p), a)));
    if (!spot) {
      spot = spots[0];
      if (P.open && !P.collapsed) pnl.classList.add("tucked");
    }
    if (spot === below && fit) box.style.maxHeight = `${Math.max(120, innerHeight - below[1] - 8)}px`;
    box.style.left = `${Math.max(8, spot[0])}px`;
    box.style.top = `${spot[1]}px`;
  }

  function placeField() {
    if (!shadow) return;
    const el = fieldEl || choiceEl;
    const box = shadow.getElementById("field") || shadow.getElementById("pill");
    if (!box || !el) return;
    if (!el.isConnected) return fieldEl ? closeField() : closeChoice();
    placeNear(box, el, box.id === "field");
  }
  window.addEventListener("scroll", () => requestAnimationFrame(placeField),
                          { passive: true, capture: true });
  window.addEventListener("resize", () => requestAnimationFrame(placeField));
  // A dropdown's list opens and closes on clicks and keys; move clear of it.
  for (const t of ["click", "keyup"]) {
    document.addEventListener(t, () => setTimeout(placeField, 60), true);
  }

  function div(className, textContent) {
    return Object.assign(document.createElement("div"),
                         { className, textContent });
  }

  async function askServer(type, payload) {
    let r;
    try {
      r = await chrome.runtime.sendMessage({ type, payload });
    } catch (e) {
      r = { ok: false, error: (e && e.message) || String(e) };
    }
    if (!r || !r.ok) throw new Error((r && r.error) || "no response");
    return r.data;
  }

  async function openField(el) {
    fieldEl = el;
    const st = stateOf(el);
    renderField(el);
    if (st.info || st.busy) return;
    st.busy = "Looking in your answers...";
    renderField(el);
    try {
      st.info = await askServer("answers-field", {
        question: st.question, url: location.href, title: document.title,
        limit: st.limit, field: {
          tag: el.tagName.toLowerCase(), rows: el.rows || 0,
          height: Math.round(el.getBoundingClientRect().height),
        },
      });
      if (st.info.ready && !st.ready) st.ready = st.info.ready;
      if (!st.draft && !st.ask) useReady(el, st);
    } catch (e) {
      st.error = e.message;
    }
    st.busy = "";
    if (fieldEl === el) renderField(el);
    if ((el.value || "").trim()) scheduleCheck(el, 0);
  }

  async function draftField(el, opts = {}) {
    const st = stateOf(el);
    st.error = "";
    if (draftingOff()) { renderField(el); return; }
    st.busy = opts.revise ? "Revising with your note, about 30 seconds..."
      : st.kind === "cover note"
        ? "Writing a short note from your material and this posting..."
        : "Drafting from your material and this posting, about 30 seconds...";
    st.busyAt = Date.now();
    st.busyRevise = !!opts.revise || !!opts.again;
    renderField(el);
    // Once inserted, the user's edits in the field are what gets revised.
    const shown = joinSentences((st.draft && st.draft.sentences) || []);
    const typed = (el.value || "").trim();
    const reviseFrom = st.inserted && typed && typed !== shown.trim() ? typed : shown;
    try {
      const d = await askServer("answers-draft-field", {
        question: st.question, kind: st.kind, limit: st.limit,
        group: st.info && st.info.group ? st.info.group.title : "",
        adapt: !!opts.adapt, own_line: opts.line || "",
        skip_ask: !!(opts.skip || opts.forMe || opts.revise), url: location.href, title: document.title,
        revise_from: opts.revise ? reviseFrom : "",
        revise_note: opts.revise ? opts.note || "" : "",
        company: guessCompany(), page_text: capturePosting().text,
        story: st.story || "", facts: st.facts ? [...st.facts] : [],
        instructions: [...P.followed], hidden: P.hidden || [],
        form_text: P.formText || "", allow_ai: !!P.allowAI,
      });
      st.ask = d.ask || "";
      // Every draft and revision is a version the user can step back to.
      if (!d.ask) {
        st.versions = (st.versions && st.versions.length) ? st.versions
          : (st.draft ? [st.draft] : []);
        st.versions.push(d);
        st.at = st.versions.length - 1;
        st.note = "";
      }
      st.draft = d.ask ? null : d;
      st.adapt = !!opts.adapt;
      st.fromReady = false;
      st.checkKey = "";
      if (st.draft) scheduleCheck(el, 0);
    } catch (e) {
      st.error = e.message;
    }
    st.busy = "";
    if (fieldEl === el) renderField(el);
  }

  function insertInto(el, text, st, from) {
    // The user's click, the user's words or a draft the user has read. Editing in the field is
    // the point: what the user leaves there is what gets learned from.
    setNativeValue(el, text);
    assisted.set(el, {
      question: st.question, kind: st.kind,
      group: st.info && st.info.group ? st.info.group.title
        : (st.info && st.info.title) || "",
      inserted: text, from, sent: "",
    });
    st.inserted = true;
    st.checkKey = "";
    scheduleCheck(el, 0);
    el.focus();
  }

  function counter(text, limit) {
    const n = div("count", "");
    const parts = [];
    if (limit.chars) parts.push(`${text.length} / ${limit.chars} characters`);
    if (limit.words) {
      const w = text.trim() ? text.trim().split(/\s+/).length : 0;
      parts.push(`${w} / ${limit.words} words`);
    }
    n.textContent = parts.join(" · ");
    if (overLimit(text, limit)) n.classList.add("over");
    return parts.length ? n : null;
  }

  const SOURCE_NAMES = {
    "master": "your master file", "past answer": "an answer you sent",
    "job description": "the posting", "your line": "your line",
  };

  function renderField(el) {
    const st = stateOf(el);
    if (!st.open) {
      ui().getElementById("field")?.remove();
      const g = st.info && st.info.group;
      const p = pillBox();
      p.textContent = g && g.best ? "Your answer"
        : (st.draft || st.ready) && !st.ask && !(st.ready && st.ready.ask) ? "Draft ready"
          : "Verbatim";
      p.onclick = () => { st.open = true; renderField(el); };
      requestAnimationFrame(placeField);
      return;
    }
    ui().getElementById("pill")?.remove();
    const box = fieldBox();
    // The busy tick re-renders every second; the draft stays where the user scrolled.
    const keepTop = st.busy && st.busyRevise && st.draft
      ? (box.querySelector(".said.draft") || {}).scrollTop || 0 : 0;
    box.replaceChildren();
    const g = st.info && st.info.group;
    box.append(head(st.kind === "cover note" ? "Cover note"
                    : (g ? g.title : "Verbatim"), () => {
      closedFields.add(el);
      closeField();
    }));

    if (st.busy && st.busyRevise && st.draft) {
      // A revision keeps the draft in view, its note and button busy.
      renderDraft(el, st, box);
      const said = box.querySelector(".said.draft");
      if (said) said.scrollTop = keepTop;
      clearTimeout(st.busyTick);
      st.busyTick = setTimeout(() => { if (st.busy && fieldEl === el) renderField(el); }, 1000);
    } else if (st.busy) {
      const secs = st.busyAt ? Math.round((Date.now() - st.busyAt) / 1000) : 0;
      box.append(div("note", secs ? `${st.busy} ${secs}s in.` : st.busy));
      clearTimeout(st.busyTick);
      st.busyTick = setTimeout(() => { if (st.busy && fieldEl === el) renderField(el); }, 1000);
    } else if (recording && recording.el === el) {
      box.append(div("note", "Listening. Say it the way you would on a call,"
                     + " then press Stop."));
      const btns = div("btns", "");
      const stop = button("Stop");
      stop.onclick = () => stopSaying();
      const cancel = button("Cancel", "ghost");
      cancel.onclick = () => stopSaying(true);
      btns.append(stop, cancel);
      box.append(btns);
    } else if (st.error) {
      box.append(div("att", `Could not do that: ${st.error}`));
      const again = button("Try again", "ghost");
      again.onclick = () => {
        st.error = "";
        if (!st.info) openField(el); else renderField(el);
      };
      box.append(again);
    } else if (st.ask) {
      renderAsk(el, st, box);
    } else if (st.spoken) {
      renderSpoken(el, st, box);
    } else if (st.draft) {
      renderDraft(el, st, box);
    } else if (st.info) {
      renderStored(el, st, box);
    }
    if (!st.error) {
      const vc = div("vc", "");
      vc.id = "vc";
      box.append(vc);
      paintCheck(el);
    }
    requestAnimationFrame(placeField);
  }

  function renderStored(el, st, box) {
    const g = st.info.group;
    if (st.info.category && st.info.category !== "normal") {
      box.append(div("att", "Your call each time. Nothing is stored for this."));
      return;
    }
    if (g && g.best) {
      // The user's short or long version when the user has approved one, picked by the
      // field's size and limit; the user's best answer otherwise.
      const options = [["short", "Your short answer", g.short],
                       ["long", "Your long answer", g.long],
                       ["best", "Your best answer", g.best]]
        .filter(o => (o[2] || "").trim());
      if (!st.show) st.show = (options.find(o => o[0] === g.use) || options.find(o => o[0] === "best"))[0];
      const shown = options.find(o => o[0] === st.show) || options[options.length - 1];
      box.append(div("label", shown[1]));
      box.append(div("said", shown[2]));
      const c = counter(shown[2], st.limit);
      if (c) box.append(c);
      if (options.length > 1) {
        const next = options[(options.indexOf(shown) + 1) % options.length];
        const sw = h("button", "linkbtn", `Show ${next[1].replace("Your ", "your ")}`);
        sw.onclick = () => { st.show = next[0]; renderField(el); };
        box.append(sw);
      }
      const btns = div("btns", "");
      const insert = button(st.inserted ? "Inserted" : "Insert");
      insert.disabled = !!st.inserted;
      insert.onclick = () => {
        insertInto(el, shown[2], st, shown[0] === "best" ? "best" : shown[0]);
        renderField(el);
      };
      btns.append(insert);
      if (!draftingOff()) {
        if (st.ready && !st.ready.ask) {
          const ready = button("Show the draft", "ghost");
          ready.onclick = () => { st.draft = st.ready; st.fromReady = true; renderField(el); scheduleCheck(el, 0); };
          btns.append(ready);
        }
        const adapt = button("Adapt to this job", "ghost");
        adapt.onclick = () => draftField(el, { adapt: true });
        btns.append(adapt);
      }
      const mic = micButton(el, st);
      if (mic) btns.append(mic);
      if (!draftingOff()) pickers(el, st, box);
      box.append(btns);
      if (draftingOff()) box.append(div("att", "This posting asks for no AI, so drafting is off here. Draft anyway is at the top of the panel."));
      if (g.used && g.used.length) {
        box.append(div("used", "Sent to " + g.used.join("; ")));
      }
    } else if (draftingOff()) {
      box.append(div("att", "This posting asks for no AI, so drafting is off on this page."));
      box.append(div("note", "Write this one yourself. What you send is still saved as a version."));
      const btns = div("btns", "");
      const anyway = button("Draft anyway", "ghost");
      anyway.onclick = () => { P.allowAI = true; renderChecks(); renderField(el); readAhead(); };
      btns.append(anyway);
      const mic = micButton(el, st);
      if (mic) btns.append(mic);
      box.append(btns);
    } else {
      box.append(div("note", st.kind === "cover note"
        ? "A short note for this box, from your material and this posting."
          + " No greeting or sign-off."
        : "Nothing stored for this yet. A draft uses your master file, your"
          + " past answers and this posting."));
      if (st.limit.chars || st.limit.words) {
        box.append(div("used", `Limit: ${limitWords(st.limit)}`));
      }
      pickers(el, st, box);
      const btns = div("btns", "");
      const draft = button(st.kind === "cover note" ? "Write a note" : "Draft it for me");
      draft.onclick = () => draftField(el, { forMe: true });
      btns.append(draft);
      const mic = micButton(el, st);
      if (mic) btns.append(mic);
      box.append(btns);
    }
    if (st.inserted) {
      box.append(div("used", "Edit it in the field. What you send is saved"
                     + " as a new version."));
    }
  }

  function renderAsk(el, st, box) {
    box.append(div("label", "One thing only you can say"));
    box.append(Object.assign(document.createElement("p"),
                             { textContent: st.ask }));
    const input = document.createElement("input");
    input.className = "text";
    input.placeholder = "One line, in your own words";
    input.setAttribute("aria-label", st.ask);
    const btns = div("btns", "");
    const go = button("Write it");
    const skip = button("Leave it out", "ghost");
    go.onclick = () => {
      const line = input.value.trim();
      if (!line) return input.focus();
      st.ask = "";
      draftField(el, { line, adapt: st.adapt });
    };
    input.addEventListener("keydown", e => {
      if (e.key === "Enter") { e.preventDefault(); go.click(); }
    });
    input.value = st.askLine || "";
    input.addEventListener("input", () => { st.askLine = input.value; });
    skip.onclick = () => {
      st.ask = "";
      draftField(el, { skip: true, adapt: st.adapt });
    };
    btns.append(go, skip);
    const mic = micButton(el, st, d => { st.askLine = d.text; });
    if (mic) btns.append(mic);
    box.append(input, btns);
    setTimeout(() => input.focus(), 0);
  }

  function renderDraft(el, st, box) {
    const sentences = st.draft.sentences || [];
    const said = div("said draft", "");
    sentences.forEach((s, i) => {
      if (s.new_paragraph && i) said.append(document.createElement("br"),
                                            document.createElement("br"));
      const span = document.createElement("span");
      span.className = s.sourced ? "sent" : "sent unsourced";
      span.textContent = s.text;
      span.title = s.sourced
        ? `From ${SOURCE_NAMES[s.source] || s.source}`
          + (s.quote ? `: "${s.quote.slice(0, 120)}"` : "")
        : "No source found for this sentence";
      if (!s.sourced) {
        const cut = document.createElement("button");
        cut.className = "cut";
        cut.textContent = "×";
        cut.title = "Cut this sentence";
        cut.onclick = () => {
          const next = sentences.filter(x => x !== s);
          if (s.new_paragraph && next[i]) next[i] = { ...next[i], new_paragraph: true };
          st.draft = { ...st.draft, sentences: next };
          if (st.versions && st.versions[st.at]) st.versions[st.at] = st.draft;
          renderField(el);
        };
        span.append(cut);
      }
      said.append(span, " ");
    });
    const vs = st.versions || [];
    const top = div("vrow", "");
    top.append(div("label", st.adapt ? "Adapted for this job"
                   : st.fromReady ? "Drafted ahead, ready for you" : "Draft"));
    if (vs.length > 1) {
      // Step back to an earlier version; Insert and Copy use the one shown.
      const nav = div("vnav", "");
      const prev = h("button", "linkbtn", "‹");
      prev.setAttribute("aria-label", "Earlier version");
      prev.disabled = st.at <= 0;
      prev.onclick = () => { st.at--; st.draft = vs[st.at]; st.inserted = false; renderField(el); scheduleCheck(el, 0); };
      const next = h("button", "linkbtn", "›");
      next.setAttribute("aria-label", "Later version");
      next.disabled = st.at >= vs.length - 1;
      next.onclick = () => { st.at++; st.draft = vs[st.at]; st.inserted = false; renderField(el); scheduleCheck(el, 0); };
      nav.append(prev, h("span", "", `${st.at + 1} of ${vs.length}`), next);
      top.append(nav);
    }
    box.append(top);
    // More below: a soft fade at the bottom edge, gone at the end.
    const wrap = div("saidwrap", "");
    wrap.append(said);
    const fade = () => wrap.classList.toggle("more",
      said.scrollHeight - said.scrollTop - said.clientHeight > 1);
    said.addEventListener("scroll", fade);
    requestAnimationFrame(fade);
    box.append(wrap);
    const text = joinSentences(sentences);
    const c = counter(text, st.limit);
    if (c) box.append(c);
    if (sentences.some(s => !s.sourced)) {
      box.append(div("att", "Amber has no source. Cut it or check it before"
                     + " you insert."));
    }
    const btns = div("btns", "");
    const insert = button(st.inserted ? "Inserted" : "Insert");
    insert.disabled = !sentences.length || !!st.busy;
    insert.onclick = () => {
      insertInto(el, text, st, "draft");
      renderField(el);
    };
    const copy = button(st.copied ? "Copied" : "Copy", "ghost");
    copy.disabled = !sentences.length || !!st.busy;
    copy.onclick = async () => {
      try { await navigator.clipboard.writeText(text); st.copied = true; } catch (e) { st.copied = false; }
      renderField(el);
      setTimeout(() => { st.copied = false; if (fieldEl === el) renderField(el); }, 1500);
    };
    btns.append(insert, copy);
    const mic = micButton(el, st);
    if (mic) btns.append(mic);
    box.append(btns);
    // What should change: the user's note reworks this version into a new one.
    const note = document.createElement("input");
    note.className = "text";
    note.placeholder = "What should change?";
    note.setAttribute("aria-label", "What should change in this draft");
    note.value = st.note || "";
    note.addEventListener("input", () => { st.note = note.value; });
    // One button for another go: Try again with no note, Revise with one.
    const busy = !!st.busy;
    const label = () => busy ? (st.busyLabel || "Revising")
      : note.value.trim() ? "Revise" : "Try again";
    const revise = button(label(), "ghost");
    note.disabled = revise.disabled = busy;
    note.addEventListener("input", () => { revise.textContent = label(); });
    const go = () => {
      const n = note.value.trim();
      st.busyLabel = n ? "Revising" : "Drafting";
      // The user's line from an earlier ask stays a source for every go.
      const line = (st.askLine || "").trim();
      if (n) draftField(el, { revise: true, note: n, adapt: st.adapt, line });
      else draftField(el, { again: true, adapt: st.adapt, forMe: true, line });
    };
    revise.onclick = go;
    // Enter revises here and never reaches the form underneath.
    note.addEventListener("keydown", e => {
      if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); go(); }
    });
    const rrow = div("rrow", "");
    rrow.append(note, revise);
    box.append(rrow);
    if (busy) {
      const secs = st.busyAt ? Math.round((Date.now() - st.busyAt) / 1000) : 0;
      // One line at the card's width.
      box.append(div("used", `${st.busyLabel === "Drafting" ? "Drafting again" : "Revising with your note"} · ${secs}s`));
    }
    if (st.inserted) {
      box.append(div("used", "Edit it in the field. What you send is saved"
                     + " as a new version."));
    }
  }

  // ---------- saying an answer ----------
  // The mic records here; the server turns it into text on the Mac and
  // tidies it without adding words. Nothing goes in the box until the user
  // presses Insert.
  let recording = null;
  const MAX_SAY_MS = 3 * 60 * 1000;

  function micButton(el, st, onText) {
    if (!(st.info && st.info.mic) || !navigator.mediaDevices
        || typeof MediaRecorder === "undefined") return null;
    const b = button("Say it", "ghost");
    b.title = "Say your answer. Your words are kept.";
    // Busy renders before recording, so a take started now has no Stop.
    b.disabled = !!st.busy;
    b.onclick = () => startSaying(el, st, onText || (d => {
      st.spoken = d;
      st.showRaw = false;
    }));
    return b;
  }

  async function startSaying(el, st, onText) {
    if (recording) stopSaying(true);
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      st.error = "the mic is not available here. Allow it for this site,"
        + " or type your answer";
      renderField(el);
      return;
    }
    const rec = new MediaRecorder(stream);
    const r = { el, st, rec, stream, chunks: [], onText, cancel: false };
    rec.ondataavailable = e => { if (e.data && e.data.size) r.chunks.push(e.data); };
    rec.onstop = () => finishSaying(r);
    rec.start();
    r.timer = setTimeout(() => stopSaying(), MAX_SAY_MS);
    recording = r;
    renderField(el);
  }

  function stopSaying(cancel) {
    const r = recording;
    if (!r) return;
    recording = null;
    r.cancel = !!cancel;
    clearTimeout(r.timer);
    try { r.rec.stop(); } catch (e) { finishSaying(r); }
  }

  async function finishSaying(r) {
    r.stream.getTracks().forEach(t => t.stop());
    const st = r.st;
    if (r.cancel || !r.chunks.length) {
      if (fieldEl === r.el) renderField(r.el);
      return;
    }
    const blob = new Blob(r.chunks, { type: r.rec.mimeType || "audio/webm" });
    st.busy = "Turning what you said into text...";
    if (fieldEl === r.el) renderField(r.el);
    try {
      const audio = await new Promise((resolve, reject) => {
        const fr = new FileReader();
        fr.onload = () => resolve(String(fr.result).split(",")[1] || "");
        fr.onerror = () => reject(new Error("could not read the recording"));
        fr.readAsDataURL(blob);
      });
      r.onText(await askServer("answers-dictate", {
        audio, mime: blob.type, question: st.question, limit: st.limit,
        tidy: !draftingOff(),
      }));
    } catch (e) {
      st.error = e.message;
    }
    st.busy = "";
    if (fieldEl === r.el) renderField(r.el);
  }

  function renderSpoken(el, st, box) {
    const d = st.spoken;
    const text = st.showRaw ? d.raw : d.text;
    box.append(div("label", st.showRaw ? "What you said" : "Your words"));
    box.append(div("said", text));
    const c = counter(text, st.limit);
    if (c) box.append(c);
    box.append(div("note", d.tidied && !st.showRaw
      ? "Tidied: punctuation and filler words only. Nothing added."
      : "Exactly as you said it."));
    if (d.tidied) {
      const sw = h("button", "linkbtn", st.showRaw ? "Show it tidied"
                   : "Show what you said");
      sw.onclick = () => { st.showRaw = !st.showRaw; renderField(el); };
      box.append(sw);
    }
    const btns = div("btns", "");
    const insert = button("Insert");
    insert.onclick = () => {
      insertInto(el, text, st, "spoken");
      st.spoken = null;
      renderField(el);
    };
    const again = micButton(el, st);
    if (again) again.textContent = "Say it again";
    const back = button("Back", "ghost");
    back.onclick = () => { st.spoken = null; renderField(el); };
    btns.append(insert, ...(again ? [again] : []), back);
    box.append(btns);
  }

  function joinSentences(sentences) {
    const paras = [];
    let cur = [];
    for (const s of sentences) {
      if (s.new_paragraph && cur.length) { paras.push(cur.join(" ")); cur = []; }
      cur.push(s.text);
    }
    if (cur.length) paras.push(cur.join(" "));
    return paras.join("\n\n");
  }

  document.addEventListener("focusin", e => {
    const el = e.target;
    if (shadow && el === shadow.host) return; // a click inside the card
    if (el === fieldEl) return;
    if (!isQuestionField(el) || closedFields.has(el)) {
      const f = P.mode !== "filling" && el.matches &&
        el.matches("input, select, button, [role='combobox'], [role='radio'], [role='checkbox']")
        ? choiceFieldFor(el) : null;
      if (f) { openChoice(f); return; }
      if (choiceEl && !choiceEl.contains(el)) closeChoice();
      if (fieldEl && el.matches && el.matches("input, textarea, select")) {
        closeField();
      }
      return;
    }
    closeChoice();
    stateOf(el).open = false;
    openField(el);
  }, true);

  document.addEventListener("keydown", e => {
    if (e.key !== "Escape") return;
    if (fieldEl) closeField();
    closeChoice();
  }, true);

  // A click anywhere but the field or Verbatim's own card closes it.
  document.addEventListener("pointerdown", e => {
    const el = fieldEl || choiceEl;
    if (!el || (shadow && e.target === shadow.host)) return;
    if (el === e.target || el.contains(e.target)) return;
    if (fieldEl) closeField();
    closeChoice();
  }, true);

  // ---------- dropdowns, radios and checkboxes ----------
  //
  // A field that takes a pick never gets a written draft. When the user's saved
  // info clearly names one of its options, a pill beside it says which
  // and picks it on a click. Otherwise nothing shows. Pay, visa and EEO
  // questions never get one.

  const choiceSeen = new WeakMap(); // el -> {text, key}, or null for none

  function isChoice(f) {
    return !!(f.yesno || f.radios || f.checks || f.el.tagName === "SELECT"
      || f.descriptor.widget === "combobox" || f.descriptor.widget === "listbox");
  }

  // The innermost choice field holding this element.
  function choiceFieldFor(t) {
    const hits = collectFields().filter(f => isChoice(f) &&
      (f.el === t || f.el.contains(t) || (f.radios || f.checks || []).includes(t)));
    return hits.find(f => !hits.some(o => o !== f && f.el.contains(o.el))) || null;
  }

  // The option a stored value names, as the field shows it. Lists that
  // stay closed until opened (react-select) are matched against what is
  // open now, else the value itself goes through as typed.
  function choiceText(f, value) {
    let opts = f.descriptor.options;
    if (!opts || !opts.length) {
      const l = openList(f.el);
      opts = l ? [...l.querySelectorAll("[role='option']")].map(o => o.textContent.trim()) : [];
    }
    if (!opts.length) return value;
    return bestOption(opts.filter(Boolean), value, x => x) || "";
  }

  function closeChoice() {
    if (!choiceEl) return;
    choiceEl = null;
    if (!fieldEl) ui().getElementById("pill")?.remove();
  }

  async function openChoice(f) {
    if (choiceEl === f.el) return;
    if (fieldEl) closeField();
    choiceEl = f.el;
    ui().getElementById("pill")?.remove();
    const label = f.descriptor.label || "";
    const empty = f.el.tagName === "SELECT" ? !f.el.value : !(fieldValue(f) || "").trim();
    if (!label || GUARDED_Q.test(label) || !empty) return;
    let pick = choiceSeen.get(f.el);
    if (pick === undefined) {
      pick = null;
      try {
        const d = await askServer("plan", {
          domain: location.hostname, url: location.href, preview: true,
          fields: [{ ...f.descriptor, index: 0 }],
        });
        const step = (d.plan || []).find(x => x.index === 0 && x.action === "fill");
        if (step) {
          const v = (yesNoField(f) && thresholdAnswer(label, step.value)) || step.value;
          const text = choiceText(f, v);
          if (text) pick = { text, key: step.key };
        }
      } catch (e) {
        pick = null;
      }
      choiceSeen.set(f.el, pick);
    }
    if (choiceEl !== f.el || !pick) return;
    const p = pillBox();
    p.textContent = `Suggest: ${pick.text}`;
    p.title = "From your saved info. Click to pick it.";
    p.onclick = async () => {
      p.disabled = true;
      if (await pickChoice(f, pick)) {
        choiceSeen.set(f.el, null);
        closeChoice();
      } else {
        p.textContent = "Could not pick it. Choose it yourself.";
      }
    };
    requestAnimationFrame(placeField);
  }

  async function pickChoice(f, pick) {
    const el = f.el, kind = f.descriptor.widget;
    const ok = f.yesno ? await fillYesNo(el, pick.text)
      : f.radios ? await fillRadios(f.radios, pick.text)
        : f.checks ? await fillChecks(f.checks, pick.text)
          : el.tagName === "SELECT" ? fillSelect(el, pick.text)
            : kind ? await fillWidget(el, kind, pick.text) : false;
    if (ok) {
      // Learned from like an autofilled field: a change the user makes after
      // goes back as a correction.
      const t = { el, sig: signature(f.descriptor), key: pick.key,
                  filled: fieldValue(f) || pick.text, read: () => fieldValue(f) };
      tracked.push(t);
      el.addEventListener("change", () => {
        clearTimeout(sendTimer);
        sendTimer = setTimeout(sendCorrections, 1500);
      });
    }
    return ok;
  }

  // ---------- learning from what you send ----------

  // Every written answer on the form, whoever wrote it: you, Verbatim or
  // another tool. Fields Verbatim helped with carry its draft, so lessons
  // come from how you changed it; the rest are saved as versions only.
  // Sent once per change of text, so a submit followed by pagehide does
  // not record the same version twice.
  const typedSent = new WeakMap();

  // Characters the user typed into each box with the user's own keys. Text a tool
  // filled or the user pasted does not count, so only answers the user wrote by hand
  // are kept as samples of how the user writes.
  const keyed = new WeakMap();
  document.addEventListener("input", e => {
    const el = e.target;
    if (!e.isTrusted || !el || el.tagName !== "TEXTAREA") return;
    if (e.inputType === "insertText" || e.inputType === "insertLineBreak") {
      keyed.set(el, (keyed.get(el) || 0) + ((e.data || "").length || 1));
    }
  }, true);
  function typedByHim(el, text) {
    return !!text && (keyed.get(el) || 0) >= text.length * 0.8;
  }

  function learnItems() {
    const items = [];
    for (const [el, a] of assisted) {
      const final = (el.value || "").trim();
      if (final.length < 30 || final === a.sent) continue;
      items.push({ el, a, item: { question: a.question, answer: final,
                                  inserted: a.inserted, group: a.group,
                                  from: a.from } });
    }
    for (const f of collectFields()) {
      const el = f.el;
      if (el.tagName !== "TEXTAREA" || assisted.has(el)) continue;
      const final = (el.value || "").trim();
      const question = f.descriptor.label.trim();
      if (final.length < 30 || !question || final === typedSent.get(el)) continue;
      items.push({ el, a: null, item: { question, answer: final,
                                        typed: typedByHim(el, final) } });
    }
    return items;
  }

  function learnNow() {
    const items = learnItems();
    if (!items.length) return;
    chrome.runtime.sendMessage({ type: "answers-learn", payload: {
      items: items.map(x => x.item), url: location.href,
      title: document.title, company: guessCompany(),
      page_text: capturePosting().text,
      // Short fields too, so the site's map learns which box is which.
      // Only values already in your profile are used; anything new waits
      // for you to look at, and profile.json is never changed.
      domain: location.hostname, fields: submittedFields(),
    } }).then(r => {
      if (!r || !r.ok) throw new Error((r && r.error) || "no response");
      // Marked sent only once the server has it, so a failed send is
      // tried again on the next submit or on leaving the page.
      for (const x of items) {
        if (x.a) x.a.sent = x.item.answer;
        else typedSent.set(x.el, x.item.answer);
      }
    }).catch(e => {
      console.warn("Verbatim: could not save what you sent:", e.message || e);
    });
  }

  // A submit we saw: the form's own submit event, or a click on a button
  // that says submit or apply. Leaving the page only counts after one of
  // those, so an application you walked away from teaches nothing.
  let submitSeen = false;
  const SUBMIT_BUTTON = /^(submit|submit application|apply|apply now|send application|send)$/i;
  document.addEventListener("submit", () => { submitSeen = true; learnNow(); }, true);
  document.addEventListener("click", e => {
    const b = e.target.closest && e.target.closest("button, input[type='submit'], [role='button']");
    if (b && SUBMIT_BUTTON.test((b.textContent || b.value || "").trim())) {
      submitSeen = true;
      setTimeout(learnNow, 0);
    }
  }, true);
  window.addEventListener("pagehide", () => { if (submitSeen) learnNow(); });

  // ---------- after you submit ----------

  // The extension has no submit code and never will. This only notices
  // that a submit already happened, and then asks. Nothing is recorded
  // and nothing is learned until you press the button.
  const SUBMITTED = new RegExp(
    "thank you for (applying|your (application|interest))|"
    + "application (has been |was )?(submitted|received|complete)|"
    + "we('| ha)ve received your application|"
    + "thanks for applying", "i");

  function guessCompany() {
    const og = document.querySelector('meta[property="og:site_name"]');
    if (og && og.content.trim()) return og.content.trim();
    // Board URLs carry the employer: /outside/jobs/123, /company/example
    const seg = location.pathname.split("/").filter(Boolean)[0] || "";
    if (/greenhouse|lever|ashby|jobvite|smartrecruiters/.test(location.hostname)
        && seg && !/^(embed|jobs|job|apply|application)$/i.test(seg)) {
      return seg.replace(/[-_]+/g, " ").replace(/\b\w/g, c => c.toUpperCase());
    }
    const host = location.hostname.replace(/^(www|jobs|boards|careers|apply|job-boards)\./, "");
    return host.split(".")[0].replace(/\b\w/g, c => c.toUpperCase());
  }

  // Everything on the form as it stands, for the learning pass. Values
  // leave the page only on a submit or when you press Log it, and only to
  // the local server, which drops the guarded categories before storing
  // anything.
  function submittedFields() {
    return collectFields()
      .filter(f => {
        f._value = (fieldValue(f) || "").trim();
        return f._value && f._value.length < 200;
      })
      .map(f => ({ signature: signature(f.descriptor),
                   label: f.descriptor.label, value: f._value }));
  }

  function showApplied() {
    if (panel("applied")) return;
    const box = card("applied");
    box.append(head("Application sent", () => box.remove()));

    const msg = document.createElement("p");
    msg.textContent = "Log this application and learn from the form?";
    const input = document.createElement("input");
    input.className = "text";
    input.value = guessCompany();
    input.setAttribute("aria-label", "Company");

    const log = button("Log it");
    const no = button("Not now", "ghost");
    const btns = document.createElement("div");
    btns.className = "btns";
    btns.append(log, no);

    log.onclick = async () => {
      log.textContent = "Logging...";
      log.disabled = true;
      let r;
      try {
        r = await chrome.runtime.sendMessage({
          type: "applied",
          payload: {
            company: input.value.trim(),
            url: location.href,
            domain: location.hostname,
            fields: submittedFields(),
            answers: collectAnswers(),
          },
        });
      } catch (e) {
        r = null;
      }
      const d = r && r.ok ? r.data : null;
      msg.textContent = !d
        ? `Could not log it: ${(r && r.error) || "no response"}`
        : (d.recorded ? `Logged ${input.value.trim()}.`
                      : `Not recorded: ${d.record_error}`)
          + (d.learned ? ` Learned ${d.learned} field${d.learned > 1 ? "s" : ""}.` : "")
          + (d.answers_saved ? ` Saved ${d.answers_saved} answer${d.answers_saved > 1 ? "s" : ""}.` : "")
          + (d.queued ? ` ${d.queued} to review in the toolkit.` : "");
      input.remove();
      log.remove();
      no.textContent = "Close";
    };
    no.onclick = () => box.remove();

    box.append(msg, input, btns);
  }


  function watchForSubmit() {
    let asked = false;
    const ask = () => {
      if (asked) return;
      asked = true;
      setTimeout(showApplied, 800); // let the confirmation page settle
    };
    document.addEventListener("submit", ask, true);
    setInterval(() => {
      if (asked) return;
      // Only after the toolkit did something here, so an ordinary page
      // that happens to say "thank you" is not treated as an application.
      if (!filledAStep && !tracked.length) return;
      if (SUBMITTED.test(document.body.innerText.slice(0, 4000))) ask();
    }, 2000);
  }
  watchForSubmit();

  // ---------- capture ----------

  // How much of a block reads as a job description rather than as the page
  // around it. Navigation, related jobs and footers are mostly link text, and
  // a posting is mostly prose, so link text counts against a block hard
  // enough that a long list of links can never beat a shorter piece of
  // writing. Exported for the tests, which drive it with real posting pages.
  function blockScore(textLength, linkTextLength) {
    return textLength - 6 * linkTextLength;
  }

  const MIN_POSTING = 400;

  // A posting is written in paragraphs. A filter sidebar, a facet list or a
  // row of category labels is a column of short strings, and on a board index
  // one of those can outscore everything else, because its labels are not
  // links and so escape the penalty above.
  //
  // Counting sentence endings was tried first and is too fine a distinction:
  // real postings measured 4.8 to 8.7 per thousand characters and an Ashby
  // sidebar 3.7, so any threshold that let a real Linear posting through also
  // let the sidebar through. Counting long lines separates them properly.
  // The same pages give 20, 22, 23 and 44 lines over eighty characters for
  // real postings, against 3 for that sidebar and 2 for a job list.
  //
  // innerText keeps the line breaks the page renders, which is what makes
  // this measurable at all.
  const MIN_PARAGRAPH_LINES = 6;
  const LONG_LINE = 80;

  function readsAsProse(text) {
    let long = 0;
    for (const line of text.split("\n")) {
      if (line.trim().length > LONG_LINE) long++;
    }
    return long >= MIN_PARAGRAPH_LINES;
  }

  // How close to the best a block can score and still be preferred for being
  // deeper. An outer element always scores at least as high as the one inside
  // it, since it contains everything that one does plus the page furniture
  // around it. Taking the top score alone therefore always returns body. The
  // deepest block within this margin is the smallest one that still holds
  // essentially all the prose, which is the posting rather than the page.
  const DEPTH_MARGIN = 0.98;

  function densestBlock(root) {
    const found = [];
    const walk = (el, depth) => {
      const text = (el.innerText || "").trim();
      if (text.length >= MIN_POSTING) {
        let linkText = 0;
        for (const a of el.querySelectorAll("a")) {
          linkText += (a.innerText || "").trim().length;
        }
        found.push({ el, depth, score: blockScore(text.length, linkText) });
      }
      for (const child of el.children) walk(child, depth + 1);
    };
    walk(root, 0);
    if (!found.length) return null;
    const top = Math.max(...found.map((c) => c.score));
    if (top < MIN_POSTING) return null;
    const near = found.filter((c) => c.score >= top * DEPTH_MARGIN);
    return near.reduce((a, b) => (b.depth > a.depth ? b : a)).el;
  }

  // Some boards publish the posting as structured data. When they do it is
  // exact, so it is worth trying first, but most do not: neither Greenhouse
  // nor Ashby nor Lever emitted any on the pages checked while building this.
  function fromJsonLd() {
    for (const node of document.querySelectorAll(
        'script[type="application/ld+json"]')) {
      let data;
      try { data = JSON.parse(node.textContent); } catch (e) { continue; }
      const list = Array.isArray(data) ? data : (data["@graph"] || [data]);
      for (const item of list) {
        if (!item || item["@type"] !== "JobPosting" || !item.description) continue;
        // description is html, and the whole point is to send prose.
        const box = document.createElement("div");
        box.innerHTML = item.description;
        const text = (box.textContent || "").trim();
        if (text.length < MIN_POSTING) continue;
          return { text, how: "the posting's own structured data" };
      }
    }
    return null;
  }

  // The role, the place, the employment type and often the salary sit in a
  // header above the description, outside the block holding the prose. On an
  // Ashby posting the kept text began at "About Ramp" and had lost the role,
  // every location and "$211.4K - $290.6K" with it. A letter needs the role
  // more than any other single fact, and the user needs the salary more than
  // the letter does.
  //
  // That header is not an ancestor of the description, it is a sibling, so
  // there is no element to reach for. It is simply whatever the posting
  // container says before the description starts. Walking up from the block
  // and taking that prefix picks it up whole, and the cap keeps a walk that
  // has gone too far from dragging in the page around the posting.
  const MAX_HEADER = 500;

  function withHeading(block, text) {
    let prefix = "";
    let el = block.parentElement;
    while (el && el !== document.body && el !== document.documentElement) {
      const full = (el.innerText || "").trim();
      const at = full.indexOf(text);
      if (at < 0) break;
      if (at > MAX_HEADER) break;
      if (at > 0) prefix = full.slice(0, at).trim();
      el = el.parentElement;
    }
    if (prefix) return prefix + "\n\n" + text;

    // Nothing sat above it. Fall back to naming the role from the heading or
    // the page title, so a posting whose layout defeats the walk above still
    // says what it is for.
    const parts = [];
    const h1 = document.querySelector("h1");
    const heading = h1 ? (h1.innerText || "").trim() : "";
    if (heading && heading.length < 200 && !text.startsWith(heading)) {
      parts.push(heading);
    }
    const title = (document.title || "").trim();
    // Boards commonly put the role in both the h1 and the page title, so
    // adding each on its own merit prints it twice. If either already
    // contains the other, one of them is enough.
    const dup = parts.some((p) => p.includes(title) || title.includes(p));
    if (title && title.length < 200 && !text.includes(title) && !dup) {
      parts.push(title);
    }
    return parts.length ? parts.join("\n") + "\n\n" + text : text;
  }

  function capturePosting() {
    const found = fromJsonLd();
    if (found) {
      return { url: location.href, title: document.title,
               text: found.text.slice(0, 20000), how: found.how };
    }
    // The page around a posting is nav, cookie banner, related jobs and
    // footer. Sending all of it made the model find the description inside
    // the noise, and on a board page it could pick up a neighbouring role.
    const block = densestBlock(document.body);
    if (block && readsAsProse(block.innerText.trim())) {
      return { url: location.href, title: document.title,
               text: withHeading(block, block.innerText.trim()).slice(0, 20000),
               how: "densest block" };
    }
    // Nothing looked like a posting. Send the page, which is what this always
    // used to do, rather than refusing and leaving the user with nothing.
    return { url: location.href, title: document.title,
             text: document.body.innerText.slice(0, 20000),
             how: "whole page" };
  }

  // ---------- messages from the popup ----------

  // Every path here must answer exactly once. A listener that returns true
  // and then throws leaves the popup waiting until the channel closes, and
  // the popup can only report "no reply from the page", which says nothing
  // about what actually went wrong.
  function answerWith(work, sendResponse) {
    work().then(sendResponse,
                e => sendResponse({ error: (e && e.message) || String(e) }));
    return true;
  }

  // How much of an application form this frame actually holds. The popup
  // talks to a tab, and a message to a tab reaches every frame in it, but
  // only one reply gets through: whichever frame answers first. On a
  // careers page with an embedded Greenhouse form that used to be the
  // outer page, which has no fields, answers instantly, and reported "no
  // form fields on this page" while the iframe quietly filled itself.
  // A frame with nothing to offer now declines to answer at all, so the
  // frame holding the form is the one that replies.
  function formWeight() {
    return collectFields().length +
      [...document.querySelectorAll('input[type="file"]')].length;
  }

  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg.type === "probe-page") {
      // The popup opens knowing nothing about the page. This is what it
      // needs to say something true before you press anything.
      sendResponse({
        weight: formWeight(),
        fields: collectFields().length,
        questions: collectQuestions().length,
        company: guessCompany(),
        host: location.hostname.replace(/^www\./, ""),
        url: location.href,
      });
      return;
    }
    if (msg.type === "fill-page") {
      if (!msg.frameChosen && !formWeight()) return; // let a better frame answer
      return answerWith(runFill, sendResponse);
    }
    if (msg.type === "suggest-answers-page") {
      if (!msg.frameChosen && !collectQuestions().length) return;
      return answerWith(runSuggest, sendResponse);
    }
    if (msg.type === "save-answers-page") {
      const items = collectAnswers();
      if (!items.length) {
        if (!msg.frameChosen) return; // another frame may have the answers
        sendResponse({ error: "no typed answers found on this page" });
        return;
      }
      return answerWith(
        // The page says who these answers were for. Without it the
        // server would have to guess from the last tailoring run.
        () => chrome.runtime.sendMessage({ type: "save-answers", payload: {
          items, url: location.href, title: document.title,
          company: guessCompany() } })
          .then(r => (r && r.ok ? r.data : { error: r ? r.error : "failed" })),
        sendResponse);
    }
    if (msg.type === "panel-fill") {
      // The keyboard shortcut. Same as pressing Autofill in the panel, so
      // the results land there. A second press while filling does nothing.
      if (!msg.frameChosen && !formWeight()) return;
      showPanel({ expand: true }).then(() => {
        if (P.mode !== "filling") panelFill();
      });
      sendResponse({ ok: true });
      return;
    }
    if (msg.type === "open-panel") {
      // From the toolbar button. Only the frame holding the form opens it.
      if (!msg.frameChosen && !formWeight()) return;
      showPanel({ expand: true });
      sendResponse({ ok: true });
      return;
    }
    if (msg.type === "capture-page") {
      return answerWith(
        () => {
          const posting = capturePosting();
          return chrome.runtime.sendMessage({ type: "capture", payload: posting })
            .then(r => (r && r.ok
              ? { ok: true, how: posting.how, chars: posting.text.length }
              : { error: r ? r.error : "failed" }));
        },
        sendResponse);
    }
  });

  // ---------- the panel, on application pages ----------
  //
  // Replaces the old bottom-right offer and cover letter cards, and most of
  // what the popup used to hold: who this is for, one Autofill button, what
  // it left for you, the resume and cover letter, and the written questions.

  const P = {
    checks: null, hidden: [], formText: "", allowAI: false,
    followed: new Set(), predraft: "",
    open: false, collapsed: false, mode: "ready", result: null, total: 0,
    job: null, docs: null, tailor: null, letterMsg: "", letterAttached: false,
    settings: false, autoOpen: true, server: "",
  };

  // Settings and the per-site fold. chrome.storage when there is one (the
  // extension), memory when there is not (the test harness).
  const mem = {};
  async function getPref(key, dflt) {
    try {
      if (chrome.storage && chrome.storage.local) {
        const o = await chrome.storage.local.get(key);
        return key in o ? o[key] : dflt;
      }
    } catch (e) { /* fall through to memory */ }
    return key in mem ? mem[key] : dflt;
  }
  function setPref(key, value) {
    mem[key] = value;
    try { chrome.storage?.local?.set({ [key]: value }); } catch (e) { /* memory only */ }
  }

  function h(tag, className, text) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text != null) el.textContent = text;
    return el;
  }
  function withIcon(el, svg, after = false) {
    el.insertAdjacentHTML(after ? "beforeend" : "afterbegin", svg);
    return el;
  }

  function buildPanel() {
    const p = h("div", "off");
    p.id = "panel";
    p.setAttribute("role", "complementary");
    p.setAttribute("aria-label", "Verbatim");

    const top = h("div", "top");
    const brand = h("div", "brand");
    brand.innerHTML = LOGO;
    brand.append("Verbatim");
    const lamp = h("span", "lamp");
    lamp.id = "lamp";
    lamp.title = "Verbatim server";
    brand.append(lamp);
    const gear = withIcon(h("button", "iconbtn"), ICONS.gear);
    gear.title = "Settings";
    gear.onclick = () => { P.settings = !P.settings; renderSettings(); };
    const fold = withIcon(h("button", "iconbtn round fold"), ICONS.right);
    fold.title = "Fold this away";
    fold.onclick = () => setCollapsed(true);
    top.append(brand, h("span", "spacer"), gear, fold);

    const body = h("div", "body");
    const settings = h("div", "box");
    settings.id = "settings";
    settings.hidden = true;
    const stack = h("div");
    stack.id = "stack";
    const ctx = h("div", "box");
    ctx.id = "ctx";
    const zone = h("div");
    zone.id = "fillzone";
    const rows = h("div", "list");
    rows.id = "rows";
    const qs = h("div", "list");
    qs.id = "questions";
    const foot = h("a", "foot", "Open Verbatim");
    foot.href = SERVER;
    foot.target = "_blank";
    foot.rel = "noopener";
    const checks = h("div");
    checks.id = "checks";
    body.append(settings, checks, stack, ctx, zone, rows, qs, foot);
    p.append(top, body);

    const tab = h("button");
    tab.id = "tab";
    tab.hidden = true;
    tab.title = "Open Verbatim";
    tab.innerHTML = LOGO;
    tab.onclick = () => { if (!tab.dataset.dragged) setCollapsed(false); };
    dragTab(tab);

    shadow.append(p, tab);
  }

  // The folded tab moves up and down the right edge. A press that travels
  // more than a few pixels is a drag, not a click. Stored as a share of the
  // window height so it lands in the same spot on any screen. Docked to the
  // bottom on narrow screens, where it stays put.
  function dragTab(tab) {
    const place = y => {
      const max = innerHeight - tab.offsetHeight - 8;
      tab.style.top = Math.max(8, Math.min(max, y)) + "px";
      tab.style.bottom = "auto";
    };
    getPref("tabY", null).then(f => {
      if (typeof f === "number" && !narrow()) requestAnimationFrame(() => place(f * innerHeight));
    });
    let start = null;
    tab.addEventListener("pointerdown", e => {
      if (narrow() || e.button !== 0) return;
      start = { y: e.clientY, top: tab.getBoundingClientRect().top, moved: false };
      delete tab.dataset.dragged;
      tab.setPointerCapture(e.pointerId);
    });
    tab.addEventListener("pointermove", e => {
      if (!start) return;
      const dy = e.clientY - start.y;
      if (!start.moved && Math.abs(dy) < 5) return;
      start.moved = true;
      place(start.top + dy);
    });
    tab.addEventListener("pointerup", () => {
      if (start && start.moved) {
        tab.dataset.dragged = "1";
        setPref("tabY", tab.getBoundingClientRect().top / innerHeight);
        setTimeout(() => delete tab.dataset.dragged, 0);
      }
      start = null;
    });
  }

  // ---- open, fold, dock ----

  async function showPanel(opts = {}) {
    ui();
    if (!P.open) {
      P.open = true;
      loadTheme();
      P.autoOpen = await getPref("autoOpen", true);
      // A phone starts folded, so the sheet never lands on the form unasked.
      P.collapsed = narrow() ? true
        : !!(await getPref("collapsed:" + location.hostname, false));
      renderAll();
      refreshPanelData();
    }
    if (opts.expand) P.collapsed = false;
    layout();
  }

  function setCollapsed(c) {
    P.collapsed = c;
    if (!narrow()) setPref("collapsed:" + location.hostname, c);
    layout();
  }

  function layout() {
    if (!shadow) return;
    const p = shadow.getElementById("panel");
    const tab = shadow.getElementById("tab");
    const show = P.open && !P.collapsed;
    p.classList.toggle("off", !show);
    tab.hidden = !(P.open && P.collapsed);
    dock(show && !narrow());
    requestAnimationFrame(placeField);
  }

  // Narrow the page while the panel is open, the way Jobright does, so the
  // panel sits beside the form rather than on top of it.
  function dock(on) {
    const de = document.documentElement;
    if (!document.getElementById("verbatim-dock-style")) {
      const s = document.createElement("style");
      s.id = "verbatim-dock-style";
      s.textContent = `html[data-verbatim-dock]{width:calc(100% - ${PANEL_W}px)`
        + ` !important;min-width:0 !important;}`;
      (document.head || de).appendChild(s);
    }
    if (on) de.setAttribute("data-verbatim-dock", "");
    else de.removeAttribute("data-verbatim-dock");
  }
  window.addEventListener("resize", () => { if (P.open) layout(); });

  // ---- what the panel knows about this page ----

  // "Senior Product Designer at Northwind", "Job Application for X at Y".
  function pageJob() {
    const t = (document.title || "").trim();
    const m = t.match(/^(?:job application for\s+)?(.+?)\s+at\s+(.+?)(?:\s+[|\-–]\s+.*)?$/i);
    if (m) return { title: m[1].trim(), company: m[2].trim() };
    const h1 = document.querySelector("h1");
    const title = h1 && h1.textContent.trim().length < 120 ? h1.textContent.trim() : "";
    return { title, company: "" };
  }

  function normUrl(u) {
    try {
      const x = new URL(u);
      return (x.host + x.pathname).toLowerCase()
        .replace(/\/(apply|application)\/?$/, "").replace(/\/$/, "");
    } catch (e) {
      return "";
    }
  }

  // This posting in the job watcher's list, if it is there. The watcher is
  // still being built, so a missing route or an empty list means no pill,
  // never a made up one.
  async function watchMatch() {
    let d;
    try { d = await askServer("watch-jobs", {}); } catch (e) { return null; }
    const here = normUrl(location.href);
    return (d && d.jobs || []).find(j => {
      if (!j || !j.url) return false;
      if (normUrl(j.url) === here) return true;
      const pid = String(j.id || "").split(":").pop();
      return pid.length >= 6 && location.href.includes(pid);
    }) || null;
  }

  function postedText(age) {
    if (!age) return "";
    if (age === "today") return "Posted today";
    const m = age.match(/^(\d+)(d|w|mo)$/);
    if (!m) return "";
    const n = +m[1];
    const unit = { d: "day", w: "week", mo: "month" }[m[2]];
    return `Posted ${n} ${unit}${n === 1 ? "" : "s"} ago`;
  }

  async function refreshPanelData() {
    const jobs = [
      watchMatch().then(j => { P.job = j; renderCtx(); }),
      askServer("latest-docs", docsPage()).then(d => { P.docs = d; renderRows(); },
                                        () => { P.docs = { found: false, reason: "unknown" }; renderRows(); }),
      askServer("tailor-status", {}).then(s => {
        P.server = s.status === "running" ? "working" : "on";
        P.tailor = s;
        if (s.status === "running") pollTailor("resume");
        renderLamp();
        renderRows();
      }, () => { P.server = "off"; renderLamp(); }),
      askServer("tailor-status", { mode: "letter" }).then(s => {
        P.letterJob = s;
        if (s.status === "running") pollTailor("letter");
        renderRows();
      }, () => {}),
      checkSeen(),
      readAhead(),
    ];
    await Promise.allSettled(jobs);
  }

  // Have you been here before. Same wording as the popup had: the same
  // posting is definite, the same company is a softer signal.
  async function checkSeen() {
    let d;
    const company = P.job ? P.job.company : (pageJob().company || guessCompany());
    try {
      d = await askServer("jobs-check", { company, url: location.href });
    } catch (e) {
      return;
    }
    if (!d || !d.available) return;
    let title = "", meta = "";
    if (d.posting) {
      title = "You already applied to this posting";
      meta = [d.posting.role, d.posting.date, d.posting.status].filter(Boolean).join(", ");
    } else if (d.company && d.company.length) {
      const n = d.company.length;
      title = `You have applied to ${company || "this company"} before`;
      meta = `${n} application${n > 1 ? "s" : ""}`
        + (d.company[0] && d.company[0].date ? `, most recently ${d.company[0].date}` : "");
    } else {
      return;
    }
    panel("seen")?.remove();
    const box = h("div", "card warn");
    box.id = "seen";
    box.append(h("div", "warn-line", title), h("div", "note", meta));
    ui().getElementById("stack").append(box);
  }

  function renderAll() {
    renderLamp();
    renderSettings();
    renderChecks();
    renderCtx();
    renderFillZone();
    renderRows();
    renderQuestions();
  }

  function renderLamp() {
    const lamp = shadow && shadow.getElementById("lamp");
    if (!lamp) return;
    lamp.dataset.state = P.server || "";
    lamp.title = P.server === "off" ? "Verbatim server is not answering"
      : P.server === "working" ? "Tailoring now" : "Verbatim server is up";
  }

  function renderSettings() {
    const box = shadow.getElementById("settings");
    box.hidden = !P.settings;
    if (!P.settings) return;
    box.replaceChildren();
    const row = h("div", "set-row");
    row.append(h("span", "", "Open this panel on application forms"));
    const sw = h("button", "switch");
    sw.setAttribute("role", "switch");
    sw.setAttribute("aria-checked", String(P.autoOpen));
    sw.setAttribute("aria-label", "Open this panel on application forms");
    sw.append(h("i"));
    sw.onclick = () => {
      P.autoOpen = !P.autoOpen;
      setPref("autoOpen", P.autoOpen);
      sw.setAttribute("aria-checked", String(P.autoOpen));
    };
    row.append(sw);
    box.append(row);
    box.append(h("div", "note", P.autoOpen
      ? "When off, open it from the Verbatim button in the toolbar."
      : "Off. Open it from the Verbatim button in the toolbar."));
    const trow = h("div", "set-row theme-row");
    trow.append(h("span", "", "Theme"));
    const seg = h("div", "seg");
    for (const [val, label] of [["system", "Auto"], ["light", "Light"], ["dark", "Dark"]]) {
      const b = h("button", "", label);
      b.setAttribute("aria-pressed", String(P.theme === val));
      b.onclick = () => {
        applyTheme(val);
        setPref("theme", val);
        askServer("ui-theme-set", { theme: val }).catch(() => {});
        renderSettings();
      };
      seg.append(b);
    }
    trow.append(seg);
    box.append(trow);
  }

  function renderCtx() {
    const box = shadow.getElementById("ctx");
    box.replaceChildren();
    const pj = pageJob();
    const j = P.job;
    const company = (j && j.company) || pj.company || guessCompany();
    const title = (j && j.title) || pj.title;
    const host = location.hostname.replace(/^www\./, "");

    const topRow = h("div", "ctx-top");
    const tile = h("div", "tile", (company[0] || "?").toUpperCase());
    const who = h("div", "who");
    who.append(h("b", "", company), h("span", "", host));
    topRow.append(tile, who);
    if (j && j.tier && ["strong", "look", "stretch"].includes(j.tier)) {
      const pill = h("div", "pill " + j.tier, j.tier_label);
      pill.title = j.reason || "From the job watcher";
      topRow.append(pill);
    }
    box.append(topRow);
    if (title) box.append(h("div", "role", title));

    const fields = collectFields().length;
    const qs = questionFields().length;
    const meta = h("div", "meta");
    const posted = postedText(j && j.age);
    const parts = [];
    if (posted) parts.push(posted);
    parts.push(`${fields} field${fields === 1 ? "" : "s"}`
      + (qs ? `, ${qs} written` : ""));
    meta.textContent = parts.join(" • ");
    box.append(meta);
    // Not on the job board yet: one press puts it there, scored like the rest.
    if (!j && P.server !== "off") {
      const add = h("button", "linkbtn", P.adding ? "Adding..." : "Add to job board");
      add.disabled = !!P.adding;
      add.onclick = async () => {
        P.adding = true; P.addError = ""; renderCtx();
        try {
          const r = await askServer("watch-add", { url: location.href,
            text: (document.body.innerText || "").slice(0, 20000) });
          P.job = r.job || null;
        } catch (e) {
          P.addError = e.message;
        }
        P.adding = false; renderCtx();
      };
      box.append(add);
      if (P.addError) box.append(h("div", "meta", `Could not add it: ${P.addError}`));
    }
  }

  function strip() {
    const s = h("div", "strip");
    s.innerHTML = ICONS.shield;
    s.append("Nothing is ever submitted. You review and send.");
    return s;
  }

  function renderFillZone() {
    const zone = shadow.getElementById("fillzone");
    zone.replaceChildren();
    if (P.mode === "results" && P.result) return renderResults(zone);

    const g = h("div", "fill-group");
    g.id = "offer";
    g.dataset.step = currentStep;
    if (filledAStep && P.mode === "ready") {
      g.append(h("div", "nextstep", "Next step of this application. Fill it too?"));
    }
    const b = h("button", "autofill", P.mode === "filling" ? "Filling..." : "Autofill");
    if (P.mode === "filling") {
      b.disabled = true;
      const bar = h("span", "bar");
      bar.append(h("i"));
      b.append(bar);
    }
    b.title = "Shortcut: Option+Shift+F";
    b.onclick = panelFill;
    g.append(b, strip());
    zone.append(g);
  }

  function renderResults(zone) {
    const r = P.result;
    const g = h("div", "fill-group");
    g.id = "results";
    const box = h("div", "box res");
    if (r.error) {
      box.append(h("div", "res-title", "Could not fill"));
      box.append(h("div", "res-note bad", `Could not fill: ${r.error}`));
    } else {
      const needs = lastNeeds.filter(n => n.el && n.el.isConnected);
      box.append(h("div", "res-title", `${r.filled} filled, `
        + (r.flagged ? `${r.flagged} need${r.flagged === 1 ? "s" : ""} you` : "nothing needs you")));
      const bar = h("div", "res-bar");
      const fill = h("i");
      const total = r.total || r.filled || 1;
      fill.style.width = `${Math.min(100, Math.round(100 * r.filled / total))}%`;
      bar.append(fill);
      box.append(bar);
      const docNote = r.uploaded
        ? `Attached ${r.uploaded} document${r.uploaded > 1 ? "s" : ""}`
          + (r.docsFor ? ` (${r.docsFor})` : "") + ". "
        : "";
      const missNote = r.mappingError
        ? `Could not identify some fields: ${r.mappingError}. Try again to`
          + " have another go at those. "
        : r.unidentified
          ? `${r.unidentified} field${r.unidentified > 1 ? "s" : ""} could not`
            + " be told apart from others on the page, so they were left alone. "
          : "";
      box.append(h("div", "res-note", `${r.total} fields on the form. ` + docNote + missNote
        + "Review everything before you submit."));
      if (needs.length) {
        const list = h("div", "needs");
        list.append(h("div", "needs-head", "Needs you"));
        for (const n of needs) {
          const item = h("button", "need");
          const text = h("span", "need-text");
          text.append(h("span", "need-label", n.label), h("span", "need-why", n.why));
          item.append(h("span", "ndot"), text);
          item.onclick = () => goTo(n.el);
          list.append(item);
        }
        box.append(list);
      }
    }
    const again = h("button", "linkbtn again", r.error ? "Try again" : "Fill again");
    again.onclick = panelFill;
    box.append(again);
    g.append(box, strip());
    zone.append(g);
  }

  // Scroll to a field and put the cursor in it. A written question then
  // opens its answer card, the same as clicking into it.
  function goTo(el) {
    if (!el || !el.isConnected) return;
    if (narrow()) setCollapsed(true);
    el.scrollIntoView({ block: "center", behavior: "smooth" });
    const target = el.matches("input, textarea, select, [tabindex], [contenteditable]")
      ? el : el.querySelector("input:not([type=hidden]), textarea, select, button");
    if (target) setTimeout(() => target.focus({ preventScroll: true }), 300);
  }

  async function panelFill() {
    P.mode = "filling";
    renderFillZone();
    let r;
    try {
      r = await runFill();
    } catch (e) {
      r = { error: (e && e.message) || String(e) };
    }
    P.result = r;
    P.mode = "results";
    if (!r.error) filledAStep = true;
    renderAll();
    const badge = lastNeeds.length;
    const tab = shadow.getElementById("tab");
    tab.querySelector(".badge")?.remove();
    if (badge) tab.append(h("span", "badge", String(badge)));
  }

  // ---- autofill info, resume, cover letter ----

  // What the server matches a tailored resume on. Inside a form's iframe
  // the frame's own address carries the job id.
  function docsPage() {
    return { url: location.href, title: document.title, standard: resumeChoice === "standard" };
  }

  // A run's steps from the server's stage: a thin bar, each step ticked
  // when done and the current one in ink. A step's typical seconds pace
  // the bar inside it, slowing as it runs long, so it never parks at 99.
  function stepsEl(s) {
    const st = s.stages || [];
    const i = Math.max(0, st.findIndex(x => x.key === s.stage));
    const total = st.reduce((a, x) => a + x.est, 0) || 1;
    const est = (st[i] || {}).est || 1, t = s.stage_elapsed || 0;
    const pct = Math.min(0.95, (st.slice(0, i).reduce((a, x) => a + x.est, 0) + est * t / (t + est * 0.15)) / total);
    const box = h("div", "tsteps");
    const bar = h("div", "tbar");
    bar.setAttribute("role", "progressbar");
    bar.setAttribute("aria-valuenow", String(Math.round(pct * 100)));
    const fill = h("i");
    fill.style.width = (pct * 100).toFixed(1) + "%";
    bar.append(fill);
    box.append(bar);
    const ol = h("ol");
    st.concat([{ label: "Done" }]).forEach((x, n) => ol.append(h("li", n < i ? "done" : n === i ? "on" : "", x.label)));
    box.append(ol, h("div", "line dim", `${s.elapsed || 0}s in. Safe to close this and check back.`));
    return box;
  }

  // Tailored or Standard, when this job has a tailored resume. Held for
  // this page visit only. resumeEls are the inputs
  // the resume went into, so a switch can swap the file in place.
  var resumeChoice = "tailored";
  const resumeEls = new Set();

  // The form's resume fields: the ones a fill attached to, plus any the
  // form shows now (a field that loaded late).
  function resumeInputs() {
    const all = [...document.querySelectorAll('input[type="file"]')].filter(el => fileKind(el) === "resume");
    return [...new Set([...resumeEls].filter(el => el.isConnected).concat(all))];
  }

  // The Attach key: the chosen resume into the resume field now, replacing
  // whatever is there, the same as a fill does.
  async function attachResumeNow() {
    P.resumeBusy = true; P.resumeDone = ""; renderRows();
    try {
      P.docs = await askServer("latest-docs", docsPage());
      if (!P.docs || !P.docs.found) throw new Error(P.docs && P.docs.reason || "no resume set");
      let n = 0;
      for (const el of resumeInputs()) {
        if (attach(el, P.docs.resume)) { resumeEls.add(el); n++; }
      }
      if (!n) throw new Error("no resume field on this form");
      P.resumeDone = P.docs.tailored ? "Attached tailored resume" : "Attached standard resume";
    } catch (e) {
      P.resumeDone = `Could not attach: ${e.message}`;
    }
    P.resumeBusy = false;
    renderRows();
  }

  async function switchResume(choice) {
    if (choice === resumeChoice) return;
    // After a submit the file already went; changing it now would mislead.
    if (submitSeen || SUBMITTED.test(document.body.innerText || "")) return;
    resumeChoice = choice;
    try {
      P.docs = await askServer("latest-docs", docsPage());
    } catch (e) {
      P.docs = { found: false, reason: "unknown" };
    }
    if (P.docs && P.docs.found) {
      let n = 0;
      for (const el of resumeEls) {
        if (el.isConnected) { try { if (attach(el, P.docs.resume)) n++; } catch (e) { /* left as is */ } }
      }
      if (n) P.resumeDone = P.docs.tailored ? "Attached tailored resume" : "Attached standard resume";
    }
    renderRows();
  }

  function coverInput() {
    return [...document.querySelectorAll('input[type="file"]')]
      .find(el => fileKind(el) === "cover_letter") || null;
  }

  function renderRows() {
    const box = shadow.getElementById("rows");
    box.replaceChildren();

    // Your autofill info
    const info = h("div", "sec");
    const ih = withIcon(h("button", "sec-head"), ICONS.user);
    ih.append(h("span", "sec-label", "Your autofill info"));
    if (lastMissing.length) {
      const dot = h("span", "dot");
      dot.title = "Some facts are missing from your profile";
      ih.append(dot);
    }
    withIcon(ih, ICONS.right, true);
    ih.onclick = () => window.open(SERVER, "_blank");
    info.append(ih);
    if (lastMissing.length) {
      info.append(h("div", "line dim", "No stored value for: "
        + [...new Set(lastMissing)].map(k => k.replace(/_/g, " ")).join(", ") + "."));
    }
    box.append(info);

    // Resume
    const res = h("div", "sec");
    const rh = withIcon(h("div", "sec-head"), ICONS.file);
    rh.append(h("span", "sec-label", "Resume"));
    res.append(rh);
    const d = P.docs;
    res.append(h("div", "line", !d ? "Checking which file attaches"
      : d.found ? (d.tailored ? `Attaches ${d.source}` : `Attaches your standard resume (${d.source})`)
      : `No resume set: ${d.reason || "none set"}`));
    if (d && (d.tailored || d.tailored_available)) {
      const seg = h("div", "seg");
      for (const [val, label] of [["tailored", "Tailored"], ["standard", "Standard"]]) {
        const b = h("button", "", label);
        b.setAttribute("aria-pressed", String(resumeChoice === val));
        b.onclick = () => switchResume(val);
        seg.append(b);
      }
      res.append(seg);
    }
    // Always offered: a form can load its file field after the panel.
    if (d && d.found) {
      const at = h("button", "soft", P.resumeBusy ? "Attaching" : "Attach");
      at.disabled = !!P.resumeBusy;
      at.onclick = attachResumeNow;
      res.append(at);
    }
    if (P.resumeDone) res.append(h("div", "line dim", P.resumeDone));
    const t = P.tailor || {};
    // The letter follows its own run, or a run that made both (the Tailor
    // page); a resume-only run has no letter.
    const lj = P.letterJob || {};
    const lt = lj.status && lj.status !== "idle" ? lj : (t.mode === "resume" ? {} : t);
    if (t.status === "starting") {
      res.append(h("div", "line dim", "Starting..."));
    } else if (t.status === "running") {
      res.append(stepsEl(t));
    } else if (t.status === "done") {
      res.append(h("div", "line dim", "Tailored for "
        + ([t.role, t.company].filter(Boolean).join(" at ") || "this posting")));
      if (t.pdf_error) res.append(h("div", "line att", `PDF problem: ${t.pdf_error}`));
      else if (t.notes && t.notes.length) {
        res.append(h("div", "line dim", `${t.notes.length} note${t.notes.length > 1 ? "s" : ""} waiting in Verbatim.`));
      }
      const view = h("button", "soft", "View tailored resume");
      view.onclick = () => t.files && window.open(SERVER + t.files.resume_pdf, "_blank");
      res.append(view);
    } else if (t.status === "error") {
      res.append(h("div", "line att", t.error && t.error.startsWith("Could not")
        ? t.error : `Tailoring failed: ${t.error || "unknown"}`));
    }
    if (P.readNote && t.status !== "error") res.append(h("div", "line dim", P.readNote));
    if (t.status !== "running" && t.status !== "starting") {
      const make = withIcon(h("button", "soft"), ICONS.spark);
      make.append("Make a resume for this job");
      make.onclick = () => startTailor("resume");
      res.append(make);
    }
    box.append(res);

    // Cover letter: the same review as Verbatim's Tailor page (0.6.14).
    // The letter is a draft until the user approves it here or there; approving
    // makes the PDF from exactly that text and, on this form, attaches it
    // or pastes it. Nothing reaches the form before the user presses the button.
    const cl = h("div", "sec");
    const ch = withIcon(h("div", "sec-head"), ICONS.mail);
    ch.append(h("span", "sec-label", "Cover letter"));
    if (lt.status === "done" && lt.company) ch.append(h("span", "val", "written for " + lt.company));
    cl.append(ch);
    if (lt.status === "starting") {
      cl.append(h("div", "line dim", "Starting..."));
    } else if (lt.status === "running") {
      cl.append(stepsEl(lt));
    } else if (lt.status === "done" && lt.id) {
      renderLetter(cl, lt);
    } else if (lt.status === "error" && lt === lj) {
      cl.append(h("div", "line att", lt.error && lt.error.startsWith("Could not")
        ? lt.error : `Writing failed: ${lt.error || "unknown"}`));
    } else {
      cl.append(h("div", "line", "No letter yet"));
    }
    if (lt.status !== "running" && lt.status !== "starting") {
      const make = withIcon(h("button", "soft"), ICONS.spark);
      make.append(lt.status === "done" ? "Make cover letter again" : "Make cover letter");
      make.onclick = () => startTailor("letter");
      cl.append(make);
    }
    box.append(cl);
  }

  // The clipboard API needs a focused page; the old copy command does not.
  // ---- cover letter review ----

  // A textarea the form labels as the cover letter, for forms that take
  // the letter as text instead of a file.
  function coverTextbox() {
    return [...document.querySelectorAll("textarea")]
      .find(el => /cover[\s_-]*letter/i.test([labelFor(el), el.name, el.id, el.placeholder].join(" "))) || null;
  }

  const LR_NOTE = "What should change? For example, shorter, or lead with your most recent role";
  const lrSame = (a, b) => (a || "").replace(/\s+/g, " ").trim() === (b || "").replace(/\s+/g, " ").trim();

  async function loadLetter(id) {
    const L = P.letter;
    try {
      const st = await askServer("letter-get", { id });
      P.letter = { id, versions: st.versions, approved: st.approved,
                   at: st.versions.length - 1, drafts: {}, open: !st.approved,
                   done: L && L.id === id ? L.done : "", msg: "" };
    } catch (e) {
      P.letter = { id, error: "Unable to open that letter." };
    }
    renderRows();
  }

  function renderLetter(cl, t) {
    const L = P.letter;
    if (!L || L.id !== t.id) {
      P.letter = { id: t.id, loading: true };
      loadLetter(t.id);
      cl.append(h("div", "line dim", "Loading"));
      return;
    }
    if (L.loading) { cl.append(h("div", "line dim", "Loading")); return; }
    if (L.error) { cl.append(h("div", "line att", L.error)); return; }
    const file = coverInput(), box = file ? null : coverTextbox();
    const v = L.versions[L.at];
    const latest = L.at === L.versions.length - 1;
    const text = L.drafts[L.at] ?? v.text;
    const apV = L.approved ? L.versions.find(x => x.n === L.approved) : null;

    if (!L.open && apV) {
      // Approved: nothing to review, only to use.
      cl.append(h("div", "line", L.done ? `PDF ready • ${L.done}` : "Approved"));
      const pair = h("div", "pair");
      if ((file || box) && !L.done) {
        const use = h("button", "soft", file ? "Attach" : "Paste");
        use.onclick = () => useLetter(apV.text, file, box);
        pair.append(use);
      } else if (!file && !box) {
        const copy = h("button", "soft", "Copy text");
        copy.onclick = async () => { copy.textContent = (await copyText(apV.text)) ? "Copied" : "Could not copy"; };
        const open = h("button", "soft", "Open PDF");
        open.onclick = () => window.open(SERVER + "/output/" + encodeURIComponent(L.id) + "/cover-letter.pdf", "_blank");
        pair.append(copy, open);
      }
      if (pair.childNodes.length) cl.append(pair);
      if (L.lesson) cl.append(h("div", "line dim", "Saved as a letter lesson"));
      const again = h("button", "plainlink", "Change it");
      again.onclick = () => { L.open = true; renderRows(); };
      cl.append(again);
      if (L.msg) cl.append(h("div", "line att", L.msg));
      return;
    }

    // The review: stepper, source line, the letter, Change it, approve.
    const step = h("div", "lr-step");
    const prev = h("button", "lr-arrow", "‹"), next = h("button", "lr-arrow", "›");
    prev.disabled = L.at <= 0 || !!L.busy; next.disabled = latest || !!L.busy;
    prev.onclick = () => { L.at--; renderRows(); };
    next.onclick = () => { L.at++; renderRows(); };
    step.append(prev, h("span", "lr-ver", `Version ${L.at + 1} of ${L.versions.length}`), next);
    cl.append(step);
    const src = v.source === "note" ? "Revised from your note: " + (v.note || "")
      : v.source === "edit" ? "Your edits" : "First draft";
    const srcEl = h("div", "line dim lr-src");
    cl.append(srcEl);
    const ta = h("textarea", "lr-text");
    ta.value = text;
    cl.append(ta);
    const stale = h("div", "line att", "Text changed since the PDF. Approve again to update it.");
    cl.append(stale);

    cl.append(h("div", "lr-label", "Change it"));
    const note = h("textarea", "lr-note");
    note.placeholder = LR_NOTE;
    note.value = L.note || "";
    note.oninput = () => { L.note = note.value; };
    cl.append(note);
    const rev = h("button", "soft", L.busy === "revise" ? "Revising" : "Revise");
    rev.disabled = !!L.busy;
    rev.onclick = async () => {
      if (!(L.note || "").trim()) return;
      L.busy = "revise"; L.msg = ""; renderRows();
      try {
        const st = await askServer("letter-revise", { id: L.id, text: ta.value, note: L.note });
        Object.assign(L, { versions: st.versions, approved: st.approved, at: st.versions.length - 1, drafts: {}, note: "" });
      } catch (e) { L.msg = "Unable to draft the letter. Try again."; }
      L.busy = ""; renderRows();
    };
    cl.append(rev);

    const go = h("button", "lr-go");
    let older = false;
    // Typing only touches these few labels, so the box keeps its focus.
    const sync = () => {
      const now = ta.value;
      if (lrSame(now, v.text)) delete L.drafts[L.at]; else L.drafts[L.at] = now;
      srcEl.textContent = src + (lrSame(now, v.text) ? "" : " • Edited");
      stale.hidden = !(apV && !lrSame(now, apV.text));
      older = !latest && lrSame(now, v.text);
      go.textContent = L.busy === "approve" ? "Making PDF" : older ? "Use this version"
        : file ? "Approve and attach" : box ? "Approve and paste" : "Approve and make PDF";
    };
    ta.oninput = sync;
    sync();
    go.disabled = !!L.busy;
    go.onclick = async () => {
      L.busy = older ? "save" : "approve"; L.msg = ""; renderRows();
      try {
        if (older) {
          const st = await askServer("letter-save", { id: L.id, text: ta.value });
          Object.assign(L, { versions: st.versions, at: st.versions.length - 1, drafts: {} });
        } else {
          const st = await askServer("letter-approve", { id: L.id, text: ta.value });
          Object.assign(L, { versions: st.versions, approved: st.approved, at: st.versions.length - 1, drafts: {}, open: false });
          const final = st.versions.find(x => x.n === st.approved).text;
          if (file || box) await useLetter(final, file, box);
          askServer("letter-learn", { id: L.id })
            .then(r => { if (r.lessons && r.lessons.length) { L.lesson = true; renderRows(); } }, () => {});
        }
      } catch (e) {
        L.msg = older ? "Unable to save. Try again." : "Unable to make the PDF. Try again.";
      }
      L.busy = ""; renderRows();
    };
    cl.append(go);
    if (L.msg) cl.append(h("div", "line att", L.msg));
  }

  // Puts the approved letter on the form: the PDF into a file upload, or
  // the text into a cover letter box.
  async function useLetter(text, file, box) {
    const L = P.letter;
    try {
      if (file) {
        const dd = await askServer("letter-doc", { id: L.id });
        if (!dd.found || !attach(file, dd.doc)) throw new Error(dd.reason || "not attached");
        const hostEl = visibleHost(file);
        if (hostEl) { hostEl.style.outline = ""; hostEl.title = ""; }
        lastNeeds = lastNeeds.filter(n => n.el !== hostEl);
        L.done = "Attached";
      } else if (box) {
        setNativeValue(box, text);
        L.done = "Pasted";
      }
    } catch (e) {
      L.msg = `Could not attach: ${e.message}. Add it yourself.`;
    }
    renderRows();
  }

  async function copyText(text) {
    try { await navigator.clipboard.writeText(text); return true; } catch (e) { /* fall through */ }
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.cssText = "position:fixed;top:0;left:0;opacity:0;";
    document.body.append(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { /* not allowed here */ }
    ta.remove();
    return ok;
  }

  // Each button makes only its own half: "resume" or "letter". Both read
  // this page's posting, captured fresh.
  async function startTailor(mode) {
    const key = mode === "letter" ? "letterJob" : "tailor";
    P[key] = { status: "starting", mode };
    if (mode === "letter") { P.letterMsg = ""; P.letterAttached = false; }
    else P.readNote = "";
    renderRows();
    try {
      const posting = capturePosting();
      await askServer("capture", posting);
      // Say what was actually read: the posting itself, or the whole page
      // as a fallback when no posting could be found.
      if (mode !== "letter") P.readNote = posting.how
        ? `Read ${posting.text.length} characters from ${posting.how}.` : "";
      const s = await askServer("tailor-start", { mode });
      if (s && s.already_running && mode === "letter") P.letterMsg = "Already writing one. Hang on.";
    } catch (e) {
      P[key] = { status: "error", mode, error: `Could not start: ${e.message}` };
      renderRows();
      return;
    }
    pollTailor(mode);
  }

  // Back from the Tailor page: pick up versions or an approval made there.
  addEventListener("focus", () => {
    const id = P.letter && P.letter.id;
    if (id && !P.letter.busy && [P.letterJob, P.tailor].some(j => j && j.status === "done" && j.id === id)) loadLetter(id);
  });

  const tailorPolls = {};
  function pollTailor(mode = "resume") {
    if (tailorPolls[mode]) return;
    const key = mode === "letter" ? "letterJob" : "tailor";
    const tick = async () => {
      let s;
      try { s = await askServer("tailor-status", mode === "letter" ? { mode } : {}); } catch (e) { return; }
      P[key] = s;
      const busy = [P.tailor, P.letterJob].some(j => j && j.status === "running");
      P.server = busy ? "working" : "on";
      if (s.status !== "running") {
        clearInterval(tailorPolls[mode]);
        tailorPolls[mode] = null;
        // A fresh resume for this page is now the tailored one to attach.
        if (mode !== "letter" && s.status === "done")
          askServer("latest-docs", docsPage()).then(d => { P.docs = d; renderRows(); }, () => {});
        if (mode === "letter" && P.letterMsg === "Already writing one. Hang on.") P.letterMsg = "";
      }
      renderLamp();
      renderRows();
    };
    tick();
    tailorPolls[mode] = setInterval(tick, 3000);
  }

  // ---- written questions ----

  function questionFields() {
    return [...document.querySelectorAll("textarea, input")]
      .filter(visible).filter(isQuestionField);
  }

  function renderQuestions() {
    const box = shadow.getElementById("questions");
    const els = questionFields();
    box.replaceChildren();
    box.hidden = !els.length;
    if (!els.length) return;
    const sec = h("div", "sec");
    const sh = withIcon(h("div", "sec-head"), ICONS.chat);
    sh.append(h("span", "sec-label", "Written questions"),
              h("span", "val", `${els.length} on this page`));
    sec.append(sh);
    const list = h("div", "qlist");
    for (const el of els) {
      const st = stateOf(el);
      const item = h("button", "qitem");
      const has = (el.value || "").trim();
      item.append(h("span", "qtext", st.question.replace(/\s*\*\s*$/, "")),
                  h("span", "qstate" + (has || st.ready ? " done" : ""),
                    has ? "Has text" : st.ready ? "Draft ready"
                      : st.kind === "cover note" ? "Cover note" : "Empty"));
      item.onclick = () => goTo(el);
      list.append(item);
    }
    sec.append(list);
    const pair = h("div", "pair");
    const suggest = h("button", "soft", "Suggest my answers");
    suggest.onclick = async () => {
      suggest.disabled = true;
      suggest.textContent = "Looking...";
      let r;
      try { r = await runSuggest(); } catch (e) { r = { error: e.message }; }
      suggest.disabled = false;
      suggest.textContent = "Suggest my answers";
      if (r.error) setQNote(r.error, true);
      else if (r.offered) {
        setQNote(`Showed ${r.offered} of ${r.asked} question${r.asked > 1 ? "s" : ""}.`
          + " Insert what fits, then edit it.");
      } else {
        setQNote(`Nothing stored yet for the ${r.asked} question`
          + `${r.asked > 1 ? "s" : ""} on this page.`);
      }
    };
    const save = h("button", "soft", "Save my answers");
    save.onclick = async () => {
      const items = collectAnswers();
      if (!items.length) return setQNote("no typed answers found on this page", true);
      setQNote("Reading your answers...");
      let r;
      try {
        r = await askServer("save-answers", { items, url: location.href,
          title: document.title, company: guessCompany() });
      } catch (e) {
        return setQNote(`Failed: ${e.message}`, true);
      }
      setQNote(`Saved ${r.saved} to the answer repo`
        + (r.skipped ? `, skipped ${r.skipped}` : "")
        + (r.recorded_under ? ` (under ${r.recorded_under})` : "") + ".");
    };
    pair.append(suggest, save);
    sec.append(pair);
    const note = h("div", "line dim");
    note.id = "qnote";
    note.hidden = true;
    sec.append(note);
    const slot = h("div");
    slot.id = "qslot";
    sec.append(slot);
    box.append(sec);
  }

  function setQNote(text, bad) {
    const n = shadow.getElementById("qnote");
    if (!n) return;
    n.hidden = false;
    n.className = bad ? "line att" : "line dim";
    n.textContent = text;
  }

  // Keep the written questions' state current as you type.
  let qTimer = null;
  document.addEventListener("input", e => {
    if (!P.open || (shadow && e.target === shadow.host)) return;
    clearTimeout(qTimer);
    qTimer = setTimeout(renderQuestions, 600);
  }, true);

  // Workday and friends render late, and they are wizards: My Information,
  // then My Experience, then Voluntary Disclosures, all without a page
  // load. Offering once per page load meant the offer appeared on step one
  // and never again. So watch for the form changing under us and treat a
  // new set of fields as a new step: the panel goes back to its Autofill
  // button for it.
  //
  // The key deliberately avoids labelFor, which walks the DOM per field.
  // This runs every couple of seconds for as long as the tab is open.
  function stepKey() {
    // Only what is on screen. A wizard keeps every step in the DOM and
    // hides the ones you are not on, so counting all of them gives the
    // same key on every step and the offer never comes back.
    const els = [...document.querySelectorAll(
      "input, textarea, select, [role='combobox'], [aria-haspopup='listbox']")]
      .filter(e => e.getClientRects().length);
    const sample = els.slice(0, 6)
      .map(e => e.name || e.id || e.getAttribute("aria-labelledby") || "")
      .join(",");
    return `${location.href}|${els.length}|${sample}`;
  }

  let currentStep = "";
  let tries = 0;
  setInterval(async () => {
    const key = stepKey();
    if (key !== currentStep) {
      // A new step, or the first render finishing.
      currentStep = key;
      tries = 0;
      // A pick that reveals a follow-up question changes the field count
      // too. While fields this fill set are still on screen, it is the
      // same step, and the results stay up.
      const sameStep = P.mode === "results" && tracked.some(t =>
        t.el.isConnected && t.el.getClientRects().length);
      if (P.open) {
        if (P.mode === "results" && !sameStep) P.mode = "ready";
        renderCtx();
        if (!sameStep) renderFillZone();
        renderQuestions();
      }
    }
    if (P.open) return;
    if (tries++ > 8) return;
    if (collectFields().length < 5) return;
    if (!(await getPref("autoOpen", true))) return;
    if (!P.open) showPanel();
  }, 1500);

  // Automated test hook. Only replica pages served by the local toolkit
  // declare this meta tag; real sites never will. It runs the same
  // runFill() the offer and popup use, then dumps results into the DOM
  // for the headless test runner to read.
  // ?noauto skips it, so a screenshot can show the panel before any fill.
  const autotest = document.querySelector('meta[name="job-toolkit-autotest"]');
  if (autotest && !/[?&]noauto\b/.test(location.search)) {
    setTimeout(async () => {
      let result = await runFill();
      // A wizard replica: walk it the way a person would, filling each step,
      // so the report covers every step rather than only the first.
      if (autotest.content === "steps") {
        const steps = [result];
        for (let i = 0; i < 4; i++) {
          const before = stepKey();
          const next = [...document.querySelectorAll("button, [role='button']")]
            .find(b => /^(next|continue|save and continue|review)$/i
              .test((b.textContent || "").trim()));
          if (!next) break;
          next.click();
          const moved = await waitFor(() => stepKey() !== before, 3000);
          if (!moved) break;
          steps.push(await runFill());
        }
        // Give the watcher a tick or two to notice the last step and put
        // the offer back, which is the behaviour being tested.
        await waitFor(() => panel("offer"), 4000);
        result = {
          steps: steps.length,
          filled: steps.reduce((n, s) => n + (s.filled || 0), 0),
          flagged: steps.reduce((n, s) => n + (s.flagged || 0), 0),
          total: steps.reduce((n, s) => n + (s.total || 0), 0),
          perStep: steps.map(s => s.error
            ? { error: s.error }
            : { filled: s.filled, flagged: s.flagged, total: s.total }),
          offerReturned: !!panel("offer"),
        };
      }
      if (autotest.content === "posting") {
        // What the panel shows at the top, and whether it would read the
        // questions ahead. Read from the same state the panel renders.
        await postingCheck();
        await readAhead();
        ui();
        renderChecks();
        const c = P.checks || {};
        result.posting = {
          no_ai: (c.no_ai || []).map(x => x.quote),
          instructions: (c.instructions || []).map(x => x.quote),
          traps: (c.traps || []).map(x => x.quote),
          hidden_found: (P.hidden || []).length,
          predraft: P.predraft || "",
          shown: shadow ? ["noai", "instructions", "traps"]
            .filter(id => shadow.getElementById(id)) : [],
        };
      }
      if (autotest.content === "answers") {
        // The written questions as the answer card sees them: which fields
        // get a card, what limit each one has, and which is a cover note.
        result.questions = [...document.querySelectorAll("textarea, input")]
          .filter(visible).filter(isQuestionField).map(el => {
            const st = stateOf(el);
            return { label: st.question.slice(0, 60), kind: st.kind,
                     limit: st.limit };
          });
      }
      const report = {
        page: location.pathname,
        result,
        fields: collectFields().map(f => ({
          sig: signature(f.descriptor),
          tag: f.descriptor.widget || f.descriptor.tag,
          label: f.descriptor.label.slice(0, 80),
          value: fieldValue(f).slice(0, 120),
          outline: f.el.style.outline || "",
        })).concat(
          [...document.querySelectorAll('input[type="file"]')].map(el => ({
            sig: "file:" + (fileKind(el) || "unknown"),
            tag: "file",
            value: el.files[0] ? el.files[0].name : "",
            outline: "",
          }))),
      };
      // test pages are served by the toolkit itself, so this is same-origin
      fetch("/api/test/report", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(report),
      });
    }, 400);
  }
})();
