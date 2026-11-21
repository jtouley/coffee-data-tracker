// admin.registry.coffee: loads each page's data region (with its loading, slow and error states), submits facet
// filters, runs the WebAuthn ceremonies for sign-in, and every write: a confirm step (with the exact job arguments,
// the guards' refusals and a typed count where asked), one POST with the form's idempotency key, the outcome, and
// Undo for a decision. No inline script anywhere (CSP script-src 'self').
"use strict";

const b64u = {
  enc(buf) {
    const s = String.fromCharCode(...new Uint8Array(buf));
    return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  },
  dec(str) {
    const s = atob(str.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((str.length + 3) % 4));
    return Uint8Array.from(s, (c) => c.charCodeAt(0)).buffer;
  },
};

async function post(url, body) {
  const r = await fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-Requested-With": "fetch" },
    body: JSON.stringify(body || {}),
  });
  let data = {};
  try { data = await r.json(); } catch { /* an empty or non-JSON reply */ }
  if (r.status === 401) {
    location.assign(`/signin?ended=1&next=${encodeURIComponent(location.pathname + location.search)}`);
  }
  if (!r.ok) throw Object.assign(new Error(data.detail || `The server refused (${r.status}).`), { status: r.status, data });
  return data;
}

// --- data regions -------------------------------------------------------------------------------------------

// quiet: refresh in place (after a write, or a poll while a job runs) without showing the loading state again.
// titles: also refresh the title's buttons and sheets (after a write, whose forms carry the version they were made
// from); a poll leaves them, and any panel open there, alone.
function loadPart(section, quiet, titles = !quiet) {
  const url = section.dataset.part;
  const loading = section.dataset.loadingHtml || section.innerHTML;
  section.dataset.loadingHtml = loading;
  clearTimeout(section.pollTimer);
  if (!quiet) section.innerHTML = loading;
  section.setAttribute("aria-busy", "true");
  const slow = setTimeout(() => {
    const note = section.querySelector("[data-slow-note]");
    if (note) note.hidden = false;
  }, (Number(section.dataset.slow) || 8) * 1000);
  fetch(url, { credentials: "same-origin", headers: { "X-Requested-With": "fetch" } })
    .then(async (r) => {
      if (r.status === 401) {
        const next = location.pathname + location.search;
        location.assign(`/signin?ended=1&next=${encodeURIComponent(next)}`);
        return;
      }
      const html = await r.text(); // server-rendered and escaped by the template engine
      if (quiet && !r.ok) return; // keep what is shown; the next poll tries again
      section.innerHTML = html;
      if (titles) titleActions(section, !quiet);
      const poll = section.querySelector("[data-poll]");
      if (poll) section.pollTimer = setTimeout(() => loadPart(section, true), Number(poll.dataset.poll) * 1000);
      // Filters start open where there's room for them beside the results; on a phone they fold away.
      if (matchMedia("(min-width: 760px)").matches) section.querySelectorAll("[data-filters]").forEach((d) => { d.open = true; });
      modes(section);
    })
    .catch(() => {
      if (quiet) return;
      section.innerHTML =
        '<div class="state error" role="alert" data-state="error"><h2 class="section">Couldn’t reach the admin</h2>' +
        '<p class="body">The request didn’t complete: the network or the service is down.</p>' +
        '<button class="btn secondary inline mt12" type="button" data-retry>Retry</button></div>';
    })
    .finally(() => {
      clearTimeout(slow);
      section.setAttribute("aria-busy", "false");
    });
}

document.addEventListener("click", (e) => {
  const retry = e.target.closest("[data-retry]");
  if (retry) loadPart(retry.closest("[data-part]"));
});

document.addEventListener("change", (e) => {
  const sel = e.target.closest("[data-autosubmit]");
  if (sel && sel.form) sel.form.requestSubmit();
});

// --- busy buttons: the bag page's filling cup, as in the app ------------------------------------------------

const CUP =
  '<svg class="cup" viewBox="0 0 48 48" aria-hidden="true" focusable="false"><g class="stream"><rect x="22" y="1" width="2.5" height="5" rx="1.25"/><rect x="22" y="10" width="2.5" height="5" rx="1.25"/></g><g clip-path="url(#busy-cup-inside)"><rect class="pour" x="10" y="19" width="26" height="22"/></g><path class="rim" d="M11 19h24v12a9 9 0 0 1-9 9h-6a9 9 0 0 1-9-9zM35 23h2.5a4 4 0 0 1 0 8H35M8 44h30"/></svg>';

function ensureCupDefs() {
  if (document.getElementById("busy-cup-inside")) return;
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("class", "cup-defs");
  svg.setAttribute("aria-hidden", "true");
  svg.innerHTML = '<clipPath id="busy-cup-inside"><path d="M11 19h24v12a9 9 0 0 1-9 9h-6a9 9 0 0 1-9-9z"/></clipPath>';
  document.body.appendChild(svg);
}

function busy(button, on) {
  if (!button) return;
  if (on) {
    ensureCupDefs();
    button.dataset.label = button.textContent;
    button.innerHTML = CUP;
    button.append(button.dataset.busy || "Working…");
    button.setAttribute("aria-disabled", "true");
  } else if (button.dataset.label) {
    button.textContent = button.dataset.label;
    button.removeAttribute("aria-disabled");
  }
}

// --- WebAuthn ------------------------------------------------------------------------------------------------

function creationOptions(json) {
  const o = JSON.parse(json);
  o.challenge = b64u.dec(o.challenge);
  o.user.id = b64u.dec(o.user.id);
  o.excludeCredentials = (o.excludeCredentials || []).map((c) => ({ ...c, id: b64u.dec(c.id) }));
  return o;
}

function requestOptions(json) {
  const o = JSON.parse(json);
  o.challenge = b64u.dec(o.challenge);
  o.allowCredentials = (o.allowCredentials || []).map((c) => ({ ...c, id: b64u.dec(c.id) }));
  return o;
}

function registration(c) {
  return {
    id: c.id, rawId: b64u.enc(c.rawId), type: c.type,
    response: {
      clientDataJSON: b64u.enc(c.response.clientDataJSON),
      attestationObject: b64u.enc(c.response.attestationObject),
      transports: c.response.getTransports ? c.response.getTransports() : [],
    },
    clientExtensionResults: c.getClientExtensionResults(),
    authenticatorAttachment: c.authenticatorAttachment || undefined,
  };
}

function assertion(c) {
  return {
    id: c.id, rawId: b64u.enc(c.rawId), type: c.type,
    response: {
      clientDataJSON: b64u.enc(c.response.clientDataJSON),
      authenticatorData: b64u.enc(c.response.authenticatorData),
      signature: b64u.enc(c.response.signature),
      userHandle: c.response.userHandle ? b64u.enc(c.response.userHandle) : undefined,
    },
    clientExtensionResults: c.getClientExtensionResults(),
  };
}

function say(form, text) {
  const hint = form.querySelector("[data-hint]");
  if (hint) hint.textContent = text;
}

function why(err) {
  if (err && err.name === "NotAllowedError") return "The passkey prompt was closed or timed out. Try again.";
  if (err && err.name === "InvalidStateError") return "This passkey is already registered here.";
  if (err && err.status === 503) return "Sign-in is unavailable right now. Try again in a minute.";
  return (err && err.message) || "Something went wrong. Try again.";
}

const FLOWS = {
  async signin(form) {
    const begin = await post("/api/auth/signin/begin", {});
    const cred = await navigator.credentials.get({ publicKey: requestOptions(begin.options) });
    const done = await post("/api/auth/signin/finish", { ceremony: begin.ceremony, credential: assertion(cred), next: form.dataset.next });
    location.assign(done.next || "/week");
  },
  async enroll(form) {
    const begin = await post("/api/auth/enroll/begin", { token: form.token.value });
    const cred = await navigator.credentials.create({ publicKey: creationOptions(begin.options) });
    const done = await post("/api/auth/enroll/finish", { ceremony: begin.ceremony, credential: registration(cred) });
    form.token.value = "";
    form.hidden = true;
    const intro = document.querySelector("[data-enroll-intro]");
    if (intro) intro.hidden = true;
    const box = document.querySelector("[data-codes]");
    const list = box.querySelector("[data-code-list]");
    for (const code of done.codes) {
      const li = document.createElement("li");
      li.textContent = code;
      list.append(li);
    }
    box.hidden = false;
    box.focus();
  },
  async recover(form) {
    const begin = await post("/api/auth/recover/begin", { code: form.code.value });
    const cred = await navigator.credentials.create({ publicKey: creationOptions(begin.options) });
    const done = await post("/api/auth/recover/finish", { ceremony: begin.ceremony, credential: registration(cred) });
    location.assign(done.next || "/week");
  },
  async add(form) {
    const begin = await post("/api/auth/add/begin", { code: form.code ? form.code.value : null });
    const cred = await navigator.credentials.create({ publicKey: creationOptions(begin.options) });
    await post("/api/auth/add/finish", { ceremony: begin.ceremony, credential: registration(cred) });
    location.reload();
  },
};

document.addEventListener("submit", async (e) => {
  const form = e.target.closest("form[data-flow]");
  if (!form) return;
  e.preventDefault();
  const button = form.querySelector('button[type="submit"]');
  if (button && button.getAttribute("aria-disabled") === "true") return;
  if (!window.PublicKeyCredential) {
    say(form, "This browser can’t use passkeys. Use a current browser on a device with your passkey.");
    return;
  }
  say(form, "");
  busy(button, true);
  try {
    await FLOWS[form.dataset.flow](form);
  } catch (err) {
    say(form, why(err));
  } finally {
    busy(button, false);
  }
});

document.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-signout]");
  if (!b) return;
  busy(b, true);
  try {
    const done = await post(b.dataset.signout, {});
    location.assign(done.next || "/signin");
  } catch {
    busy(b, false);
  }
});

// Controls for one width only: a phone sorts with the sheet's options, a desktop by the table's headers. The other
// set is disabled, so a form sends one order.
function modes(root = document) {
  const wide = matchMedia("(min-width: 760px)").matches;
  root.querySelectorAll("[data-wide-only]").forEach((el) => { el.disabled = !wide; });
  root.querySelectorAll("[data-narrow-only]").forEach((el) => { el.disabled = wide; });
}

// A part's <template data-title> goes beside the page title: its Run a job, its "?" help, and their panels.
function titleActions(section, first = true) {
  const slot = document.querySelector("[data-title-actions]");
  if (!slot) return;
  slot.replaceChildren();
  const tpl = section.querySelector("template[data-title]");
  if (tpl) slot.append(tpl.content.cloneNode(true));
  const run = slot.querySelector("[data-run-form]");
  if (run) {
    runForm(run);
    if (first && "open" in run.dataset) run.closest("dialog").showModal(); // a link here asked for a job (Week, Release)
  }
}

// Run a job: the Job picks which fields show (the others are disabled, so they aren't sent) and where Run posts;
// Run opens the usual confirm with the plan, the guards and the window's override.
function runForm(form) {
  const pick = form.querySelector("[data-job]");
  const opt = pick.selectedOptions[0];
  const fields = (opt.dataset.fields || "").split(" ").filter(Boolean);
  form.querySelectorAll("[data-field]").forEach((l) => {
    const on = fields.includes(l.dataset.field);
    l.hidden = !on;
    l.querySelectorAll("input, select").forEach((i) => { i.disabled = !on; });
  });
  form.querySelector("[data-job-hint]").textContent = opt.dataset.hint || "";
  const site = pick.value === "site";
  form.querySelector("[data-site]").hidden = !site;
  form.querySelector('button[type="submit"]').hidden = site;
  form.dataset.write = `/api/jobs/${pick.value}/runs`;
  form.dataset.preview = `/api/jobs/${pick.value}/plan`;
  form.dataset.confirmTitle = `Start ${opt.textContent.trim()}?`;
}

document.addEventListener("change", (e) => {
  if (e.target.matches("[data-job]")) runForm(e.target.form);
});

function edges(tabs) {
  tabs.classList.toggle("more-start", tabs.scrollLeft > 1);
  tabs.classList.toggle("more-end", tabs.scrollLeft + tabs.clientWidth < tabs.scrollWidth - 1);
}
document.addEventListener("scroll", (e) => { if (e.target.matches && e.target.matches(".tabs")) edges(e.target); }, true);

// A phone shows the page's own tab (the tabs scroll sideways), and folds the filters unless some are applied.
function fit() {
  const on = document.querySelector(".tabs a.on");
  if (on) { // only as far as it takes to show the page's own tab, then mark the edges that hide more tabs
    const tabs = on.parentElement;
    const need = on.offsetLeft - tabs.offsetLeft + on.offsetWidth - tabs.clientWidth + 24;
    const first = [...tabs.children].find((a) => a.offsetLeft - tabs.offsetLeft >= need); // start on a whole tab
    tabs.scrollLeft = need > 0 && first ? first.offsetLeft - tabs.offsetLeft - 12 : 0;
    edges(tabs);
  }
  if (!matchMedia("(min-width: 760px)").matches) {
    document.querySelectorAll("details[data-filters]:not([data-applied])").forEach((d) => { d.open = false; });
  }
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("section[data-part]").forEach(loadPart);
  fit();
  matchMedia("(min-width: 760px)").addEventListener("change", () => { fit(); modes(); });
});

// --- writes ------------------------------------------------------------------------------------------------------

const $ = (root, sel) => root.querySelector(sel);

function fields(form) {
  const body = {};
  for (const [k, v] of new FormData(form)) body[k] = v;
  form.querySelectorAll("input[data-json]").forEach((i) => { body[i.name] = JSON.parse(i.value); });
  return body;
}

function reloadParts() {
  document.querySelectorAll("section[data-part]").forEach((s) => loadPart(s, true, true));
}

// The outcome line; `undo`: the URL that undoes the write (a decision's or a flag batch's), offered as Undo.
function showResult(text, undo) {
  const box = document.querySelector("[data-result]");
  if (!box) return;
  $(box, "[data-result-text]").textContent = text;
  const b = $(box, "[data-undo]");
  b.hidden = !undo;
  b.dataset.target = undo || "";
  box.hidden = false;
  clearTimeout(showResult.timer); // a message offering an action stays until dismissed (WCAG 2.2.1); others fade
  if (!undo) showResult.timer = setTimeout(() => { box.hidden = true; }, 20000);
}

function undoUrl(out) {
  if (out.decisions) return `/api/decisions/${encodeURIComponent(out.group || out.decisions[0])}/undo`;
  if (out.batch_id) return `/api/sources/flags/${encodeURIComponent(out.batch_id)}/undo`;
  return undefined;
}

function plural(n, one, many = `${one}s`) {
  return `${n.toLocaleString("en-US")} ${n === 1 ? one : many}`;
}

function describe(out) {
  if (out.runs) {
    const started = out.runs.map((r) => `${r.job} (${r.execution})`).join(", ");
    const failed = (out.errors || []).join("; ");
    return (started ? `Started ${started}.` : "Nothing started.") + (failed ? ` Not started: ${failed}` : "");
  }
  if (out.decisions) return `Recorded ${out.count} decision${out.count === 1 ? "" : "s"}. Build a draft on Release to see the change.`;
  if (out.batch_id) {
    const n = plural(out.count, "place");
    if (out.flag === "roaster") return `Marked ${n} as ${out.count === 1 ? "a roaster" : "roasters"}.`;
    if (out.flag === "not_roaster") return `Marked ${n} as not ${out.count === 1 ? "a roaster" : "roasters"}.` + (out.excluded ? ` ${plural(out.excluded, "coffee")} ${out.excluded === 1 ? "leaves" : "leave"} the catalog.` : "");
    return `Cleared the flag on ${n}.`;
  }
  if (out.restored !== undefined && out.reverted) return `Undone (${out.count}).` + (out.restored ? ` ${plural(out.restored, "coffee")} back in the catalog.` : "");
  if (out.reverted) return out.count ? `Undone (${out.count}): as it was before, from the next draft.` : "Already superseded: nothing to undo.";
  if (out.undone !== undefined) return out.undone ? `Undone: “${out.phrase}” is ${out.event === "added" ? "removed" : "back"}, from the next draft.` : "Already changed again since: nothing to undo.";
  if (out.filter_change) return `${out.event === "added" ? "Added" : "Removed"} the filter “${out.phrase}”. Build a draft on Release to see it in the catalog; Undo is under Recent changes.`;
  if (out.reviewed) return `Marked ${out.mark === "keep_excluded" ? "keep excluded" : "drop the filter"}.`;
  if (out.version_id) return `Saved rule settings ${out.version_id}. Build a draft on Release to see what changes.`;
  if ("crawl" in out) return `Crawl flag ${out.crawl ? "on" : "off"} for ${out.roaster_id}. It takes effect when a draft of it is published.`;
  return "Done.";
}

function refusal(err) {
  const reasons = err.data && err.data.reasons;
  if (reasons && reasons.length) return "Refused: " + reasons.join(" · ");
  if (err.status === 503) return err.message || "Writes are off right now.";
  return err.message || "Something went wrong. Try again.";
}

// A titled list of products, each a link to its store page with the store and Claude's call after it.
function productList(g) {
  const h = document.createElement("h3");
  h.className = "label mt12";
  h.textContent = g.title;
  const ul = document.createElement("ul");
  ul.className = "plist";
  for (const it of g.items) {
    const li = document.createElement("li");
    const name = document.createElement(it.url ? "a" : "span");
    if (it.url) {
      name.className = "ev";
      name.href = it.url; // https only: the server's safe_url
      name.target = "_blank";
      name.rel = "noreferrer noopener";
    }
    name.textContent = it.title || it.coffee_id;
    const store = document.createElement("span");
    store.className = "meta";
    // A list grouped by Claude's call says it once, in its title; a mixed one says it on each line.
    store.textContent = ` · ${it.roaster_id}` + (g.call ? "" : ` · Claude: ${it.verdict_label === "coffee" || it.verdict_label === "not_coffee" ? it.verdict_label.replace("_", " ") : "no verdict"}`);
    li.append(name, store);
    ul.append(li);
  }
  const out = [h, ul];
  if (g.more) {
    const more = document.createElement("p");
    more.className = "meta";
    more.textContent = `and ${g.more.toLocaleString("en-US")} more`;
    out.push(more);
  }
  return out;
}

function renderPlan(box, plan) {
  box.textContent = "";
  if (plan.lines) { // a preview that starts no job (a settings edit): its lines, then any refusal
    for (const line of plan.lines) {
      const p = document.createElement("p");
      p.className = "body";
      p.textContent = line;
      box.append(p);
    }
    // A keyword filter's preview: the products it moves, each with its store and Claude's call; the release, the
    // counts and the step-9 arithmetic under Details.
    for (const g of (plan.groups || []).filter((x) => !x.details)) box.append(...productList(g));
    if (plan.details) {
      const more = document.createElement("details");
      more.className = "plan-details";
      more.innerHTML = '<summary class="btn text start">Details</summary>';
      for (const line of plan.details) {
        const p = document.createElement("p");
        p.className = "meta";
        p.textContent = line;
        more.append(p);
      }
      for (const g of (plan.groups || []).filter((x) => x.details)) more.append(...productList(g));
      box.append(more);
    }
    for (const r of plan.refusals || []) {
      const x = document.createElement("p");
      x.className = "refused";
      x.textContent = `Refused: ${r}`;
      box.append(x);
    }
    return;
  }
  const p = document.createElement("p");
  p.textContent = plan.steps.length === 1 ? "This starts one Cloud Run job:" : `This starts ${plan.steps.length} Cloud Run jobs:`;
  const ul = document.createElement("ul");
  ul.className = "steps";
  for (const s of plan.steps) {
    const li = document.createElement("li");
    const job = document.createElement("strong");
    job.textContent = `Cloud Run job ${s.job}`;
    const code = document.createElement("code");
    code.textContent = s.args ? s.args.join(" ") : "(its own arguments)";
    li.append(job, code);
    ul.append(li);
  }
  box.append(p, ul);
  for (const r of plan.refusals || []) {
    const x = document.createElement("p");
    x.className = "refused";
    x.textContent = `Refused: ${r}`;
    box.append(x);
  }
  if (plan.window) {
    const w = document.createElement("p");
    w.className = "notice";
    w.textContent = `Now is inside ${plan.window}. Starting needs a reason, which is recorded.`;
    box.append(w);
  }
}

// One confirm dialog for every write form: data-confirm (text) or data-preview (the plan from the server), an
// optional typed value (data-type), then the POST. The idempotency key comes with the form, so a double click, a
// retry after a timeout, or a second tab acts once.
async function confirmWrite(form) {
  const d = document.getElementById("confirm");
  const body = $(d, "[data-confirm-body]");
  const hint = $(d, "[data-confirm-hint]");
  const go = $(d, "[data-confirm-go]");
  const typeWrap = $(d, "[data-type-wrap]");
  const overWrap = $(d, "[data-override-wrap]");
  const typed = $(d, "#confirm-type");
  const over = $(d, "#confirm-override");
  const ask = (form.dataset.ask || "").split(" ");
  const reason = $(d, "#confirm-reason");
  $(d, "[data-ask-reason]").hidden = !ask.includes("reason");
  $(d, "[data-ask-kind]").hidden = !ask.includes("kind");
  reason.value = form.dataset.reason || "";
  if ("picks" in form.dataset) { // the Catalog's ticked rows: what the decision covers, named in the sheet
    const items = picks();
    form.querySelector("input[name=items]").value = JSON.stringify(items);
    const names = items.slice(0, 5).map((i) => i.title || i.coffee_id).join("; ");
    form.dataset.confirmTitle = `${form.dataset.confirmLabel} ${items.length === 1 ? "this product" : `${items.length} products`}?`;
    form.dataset.confirmText = `${names}${items.length > 5 ? `; and ${items.length - 5} more` : ""}.`;
  }
  $(d, "[data-confirm-title]").textContent = form.dataset.confirmTitle || (form.dataset.confirmLabel ? `${form.dataset.confirmLabel}?` : "Confirm");
  go.textContent = form.dataset.confirmLabel || "Confirm";
  go.classList.toggle("danger", "danger" in form.dataset);
  go.classList.toggle("primary", !("danger" in form.dataset));
  hint.textContent = "";
  typed.value = "";
  over.value = "";
  typeWrap.hidden = !form.dataset.type;
  $(d, "[data-type-want]").textContent = form.dataset.type || "";
  overWrap.hidden = true;
  go.removeAttribute("aria-disabled");
  body.textContent = [form.dataset.confirmText, form.dataset.confirm].filter(Boolean).join(" ");
  const result = document.querySelector("[data-result]");
  if (result) result.hidden = true; // the confirm stands alone; its outcome replaces the last one
  document.querySelectorAll("details[data-rowmenu][open]").forEach((m) => { m.open = false; });
  document.querySelectorAll("dialog.panel[open]").forEach((p) => p.close()); // the confirm stands alone
  if (!d.open) d.showModal();
  delete form.dataset.previewed; // a save carries the token of the preview shown here, and no other
  if (form.dataset.preview) {
    body.textContent = "Checking what this starts…";
    go.setAttribute("aria-disabled", "true");
    try {
      const plan = await post(form.dataset.preview, fields(form));
      renderPlan(body, plan);
      if (plan.token) form.dataset.previewed = plan.token;
      if ("planDetails" in form.dataset) { // the summary in words; the jobs and their exact commands under Details
        const more = document.createElement("details");
        more.className = "plan-details";
        more.innerHTML = "<summary class=\"btn text start\">Details</summary>";
        more.append(...[...body.childNodes].filter((n) => !(n.classList && (n.classList.contains("refused") || n.classList.contains("notice")))));
        const say = document.createElement("p");
        say.className = "body";
        say.textContent = form.dataset.confirmText || "";
        body.prepend(say, more);
      }
      if (!(plan.refusals || []).length) go.removeAttribute("aria-disabled");
      overWrap.hidden = !plan.window;
    } catch (err) {
      body.textContent = refusal(err);
      return;
    }
  }
  go.onclick = async () => {
    if (go.getAttribute("aria-disabled") === "true") return;
    if (form.dataset.type && typed.value.trim() !== form.dataset.type) {
      hint.textContent = `Type ${form.dataset.type} to confirm.`;
      typed.focus();
      return;
    }
    hint.textContent = "";
    busy(go, true);
    try {
      const extra = { confirm: true, typed: typed.value.trim() || undefined, override: over.value.trim() || undefined,
        previewed: form.dataset.previewed || undefined };
      if (ask.includes("reason")) extra.reason = reason.value;
      if (ask.includes("kind")) extra.kind = d.querySelector("input[name=confirm-kind]:checked").value;
      const out = await post(form.dataset.write, { ...fields(form), ...extra });
      d.close();
      showResult(describe(out), undoUrl(out));
      reloadParts();
    } catch (err) {
      hint.textContent = refusal(err);
      if (err.data && err.data.overridable) {
        overWrap.hidden = false;
        over.focus();
      }
    } finally {
      busy(go, false);
    }
  };
}

// A one-click write (data-now: the Sources page's flags) posts at once, with Undo in its outcome line; any other
// write goes through the confirm sheet.
async function writeNow(form, button) {
  if ("picks" in form.dataset) form.querySelector("input[name=items]").value = JSON.stringify(picks());
  busy(button, true);
  try {
    const out = await post(form.dataset.write, fields(form));
    showResult(describe(out), undoUrl(out));
    reloadParts();
  } catch (err) {
    showResult(refusal(err));
  } finally {
    busy(button, false);
  }
}

// Pull now (the toast, or the action bar for the selected places): the Jobs page's confirm, with the plan and the
// window's override, through the Sources page's pull form.
function pullNow(places) {
  const form = document.getElementById("pull-form");
  if (!form || !places.length) return;
  form.querySelector("input[name=roasters]").value = places.join(",");
  form.dataset.confirmTitle = `Probe and crawl ${plural(places.length, "place")}?`;
  form.dataset.confirmText = `${places.join(", ")}. One job probes them, then crawls each storefront it finds, counted for the week; no Publish needed.`;
  confirmWrite(form);
}

document.addEventListener("click", (e) => {
  const t = e.target.closest("[data-pull-picks], [data-pull-one]");
  if (!t) return;
  pullNow(t.matches("[data-pull-one]") ? [t.dataset.pullOne] : picks().map((p) => p.roaster_id));
});

// A form with data-validate checks its fields before the confirm opens, and says what to fix under each one (the
// browser's own bubbles vanish and aren't read out reliably).
function invalid(form) {
  let first = null;
  for (const el of form.querySelectorAll("input[required]")) {
    const out = form.querySelector(`[data-field-error="${el.name}"]`);
    const v = el.value.trim();
    let msg = "";
    if (!v) msg = "Fill this in.";
    else if (el.minLength > 0 && v.length < el.minLength) msg = `At least ${el.minLength} characters.`;
    else if (el.name === "phrase" && !/\p{L}/u.test(v)) msg = "Use at least one letter.";
    else if (el.name === "phrase" && /[^\p{L}\p{N} &'.-]/u.test(v)) msg = "Only letters, digits, spaces, & ' . and -.";
    if (out) out.textContent = msg;
    if (msg) el.setAttribute("aria-invalid", "true"); // an empty aria-invalid reads as false
    else el.removeAttribute("aria-invalid");
    if (msg && !first) first = el;
  }
  if (first) first.focus();
  return Boolean(first);
}

document.addEventListener("submit", (e) => {
  const form = e.target.closest("form[data-write]");
  if (!form) return;
  e.preventDefault();
  if ("validate" in form.dataset && invalid(form)) return;
  const button = form.querySelector('button[type="submit"]');
  if (button && button.getAttribute("aria-disabled") === "true") return;
  if ("now" in form.dataset) writeNow(form, button);
  else confirmWrite(form);
});

document.addEventListener("click", (e) => {
  const close = e.target.closest("[data-close]");
  if (close) close.closest("dialog").close();
});

// --- Catalog: the ticked rows a decision covers, and undo ---------------------------------------------------

function picks() {
  return [...document.querySelectorAll("input[data-pick]:checked")].map((c) => JSON.parse(c.dataset.pick));
}

function updateBulk() {
  const bar = document.querySelector("[data-bulk]");
  if (!bar) return;
  const n = picks().length;
  bar.hidden = n === 0;
  $(bar, "[data-bulk-count]").textContent = `${n} ${bar.dataset.word || "chosen"}`;
}

document.addEventListener("change", (e) => {
  if (e.target.matches("[data-pick-all]")) {
    document.querySelectorAll("input[data-pick]").forEach((c) => { c.checked = e.target.checked; });
  }
  if (e.target.matches("[data-pick], [data-pick-all]")) updateBulk();
});

document.addEventListener("click", (e) => {
  if (!e.target.closest("[data-bulk-clear]")) return;
  document.querySelectorAll("input[data-pick], input[data-pick-all]").forEach((c) => { c.checked = false; });
  updateBulk();
});

document.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-undo]");
  if (!b || !b.dataset.target) return;
  busy(b, true);
  try {
    const out = await post(b.dataset.target, { key: crypto.randomUUID(), reason: "undo right after deciding" });
    showResult(describe(out));
    reloadParts();
  } catch (err) {
    showResult(refusal(err));
  } finally {
    busy(b, false);
  }
});

// --- Sources: the Filter / Sort panel, tapping a row to select it, and each row's ⋯ menu ----------------------------

document.addEventListener("click", (e) => {
  const open = e.target.closest("[data-panel]");
  if (open) {
    const d = document.getElementById(open.getAttribute("aria-controls"));
    const r = open.getBoundingClientRect(); // a desktop shows it as a popover under the button
    d.style.setProperty("--panel-top", `${Math.round(r.bottom + 8)}px`);
    d.style.setProperty("--panel-left", `${Math.round(Math.max(16, Math.min(r.right - 512, innerWidth - 528)))}px`);
    d.showModal();
    return;
  }
  const d = e.target.closest("dialog.panel");
  if (d && (e.target === d || e.target.closest("[data-panel-close]"))) { // the backdrop, or Close
    d.close();
    return;
  }
  // One ⋯ menu open at a time; a click elsewhere closes it.
  document.querySelectorAll("details[data-rowmenu][open]").forEach((m) => { if (!m.contains(e.target)) m.open = false; });
  const row = e.target.closest("tr[data-row]");
  if (row && !e.target.closest("a, button, input, summary, details, label")) {
    const box = row.querySelector("input[data-pick]");
    if (box) {
      box.checked = !box.checked;
      updateBulk();
    }
  }
});

document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  const m = e.target.closest && e.target.closest("details[data-rowmenu][open]");
  if (m) {
    m.open = false;
    m.querySelector("summary").focus();
  }
});

document.addEventListener("click", (e) => {
  if (e.target.closest("[data-result-close]")) e.target.closest("[data-result]").hidden = true;
});
