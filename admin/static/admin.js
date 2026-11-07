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
function loadPart(section, quiet) {
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
      const poll = section.querySelector("[data-poll]");
      if (poll) section.pollTimer = setTimeout(() => loadPart(section, true), Number(poll.dataset.poll) * 1000);
      // Filters start open where there's room for them beside the results; on a phone they fold away.
      if (matchMedia("(min-width: 760px)").matches) section.querySelectorAll("[data-filters]").forEach((d) => { d.open = true; });
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

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("section[data-part]").forEach(loadPart);
});

// --- writes ------------------------------------------------------------------------------------------------------

const $ = (root, sel) => root.querySelector(sel);

function fields(form) {
  const body = {};
  for (const [k, v] of new FormData(form)) body[k] = v;
  return body;
}

function reloadParts() {
  document.querySelectorAll("section[data-part]").forEach((s) => loadPart(s, true));
}

function showResult(text, undo) {
  const box = document.querySelector("[data-result]");
  if (!box) return;
  $(box, "[data-result-text]").textContent = text;
  const b = $(box, "[data-undo]");
  b.hidden = !undo;
  b.dataset.target = undo || "";
  box.hidden = false;
  clearTimeout(showResult.timer);
  showResult.timer = setTimeout(() => { box.hidden = true; }, 20000);
}

function describe(out) {
  if (out.runs) {
    const started = out.runs.map((r) => `${r.job} (${r.execution})`).join(", ");
    const failed = (out.errors || []).join("; ");
    return (started ? `Started ${started}.` : "Nothing started.") + (failed ? ` Not started: ${failed}` : "");
  }
  if (out.decisions) return `Recorded ${out.count} decision${out.count === 1 ? "" : "s"}. Build a draft on Release to see the change.`;
  if (out.reverted) return out.count ? `Undone (${out.count}). The classifier decides again in the next draft.` : "Already superseded: nothing to undo.";
  return "Done.";
}

function refusal(err) {
  const reasons = err.data && err.data.reasons;
  if (reasons && reasons.length) return "Refused: " + reasons.join(" · ");
  if (err.status === 503) return err.message || "Writes are off right now.";
  return err.message || "Something went wrong. Try again.";
}

function renderPlan(box, plan) {
  box.textContent = "";
  const p = document.createElement("p");
  p.textContent = plan.steps.length === 1 ? "This starts one Cloud Run job:" : `This starts ${plan.steps.length} Cloud Run jobs:`;
  const ul = document.createElement("ul");
  ul.className = "steps";
  for (const s of plan.steps) {
    const li = document.createElement("li");
    const code = document.createElement("code");
    code.textContent = s.args ? `${s.job}  ${s.args.join(" ")}` : `${s.job}  (its own arguments)`;
    li.append(code);
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
  $(d, "[data-confirm-title]").textContent = form.dataset.confirmLabel ? `${form.dataset.confirmLabel}?` : "Confirm";
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
  body.textContent = form.dataset.confirm || "";
  if (!d.open) d.showModal();
  if (form.dataset.preview) {
    body.textContent = "Checking what this starts…";
    go.setAttribute("aria-disabled", "true");
    try {
      const plan = await post(form.dataset.preview, fields(form));
      renderPlan(body, plan);
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
      const out = await post(form.dataset.write, { ...fields(form), confirm: true, typed: typed.value.trim() || undefined, override: over.value.trim() || undefined });
      d.close();
      showResult(describe(out));
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

document.addEventListener("submit", (e) => {
  const form = e.target.closest("form[data-write]");
  if (!form || form.hasAttribute("data-decide-form")) return;
  e.preventDefault();
  const button = form.querySelector('button[type="submit"]');
  if (button && button.getAttribute("aria-disabled") === "true") return;
  confirmWrite(form);
});

document.addEventListener("click", (e) => {
  const close = e.target.closest("[data-close]");
  if (close) close.closest("dialog").close();
});

// --- Catalog: decide one product or a chosen group, and undo ---------------------------------------------------

function picks() {
  return [...document.querySelectorAll("input[data-pick]:checked")].map((c) => JSON.parse(c.dataset.pick));
}

function updateBulk() {
  const bar = document.querySelector("[data-bulk]");
  if (!bar) return;
  const n = picks().length;
  bar.hidden = n === 0;
  $(bar, "[data-bulk-count]").textContent = `${n} chosen`;
}

document.addEventListener("change", (e) => {
  if (e.target.matches("[data-pick-all]")) {
    document.querySelectorAll("input[data-pick]").forEach((c) => { c.checked = e.target.checked; });
  }
  if (e.target.matches("[data-pick], [data-pick-all]")) updateBulk();
});

document.addEventListener("click", (e) => {
  if (e.target.closest("[data-bulk-clear]")) {
    document.querySelectorAll("input[data-pick], input[data-pick-all]").forEach((c) => { c.checked = false; });
    updateBulk();
    return;
  }
  const b = e.target.closest("[data-decide]");
  if (!b) return;
  const d = document.getElementById("decide");
  const items = b.hasAttribute("data-bulk-use") ? picks() : [JSON.parse(b.dataset.item)];
  if (!d || !items.length) return;
  const form = $(d, "form");
  form.elements.action.value = b.dataset.decide;
  d.items = items;
  const verb = b.dataset.decide === "exclude" ? "Exclude" : "Include";
  $(d, "[data-decide-title]").textContent = `${verb} ${items.length === 1 ? "this product" : `${items.length} products`}`;
  const names = items.slice(0, 5).map((i) => i.title || i.coffee_id).join("; ");
  $(d, "[data-decide-what]").textContent = (b.dataset.decide === "exclude"
    ? "Leave out of the catalog, even if the classifier says coffee: "
    : "Keep in the catalog as coffee, even if the classifier says not: ") + names + (items.length > 5 ? `; and ${items.length - 5} more` : "") + ".";
  $(d, "[data-decide-go]").textContent = verb;
  $(d, "[data-hint]").textContent = "";
  d.showModal();
  form.elements.reason.focus();
});

document.addEventListener("submit", async (e) => {
  const form = e.target.closest("form[data-decide-form]");
  if (!form) return;
  e.preventDefault();
  const d = form.closest("dialog");
  const go = $(form, "[data-decide-go]");
  if (go.getAttribute("aria-disabled") === "true") return;
  const kind = form.elements.kind.value;
  const items = d.items || [];
  if (items.some((i) => !i[kind])) {
    $(form, "[data-hint]").textContent = "Some of these have no version hash (an older build): decide them by coffee_id.";
    return;
  }
  const seen = Object.fromEntries(items.map((i) => [i[kind], i[`seen_${kind}`] || null]));
  busy(go, true);
  try {
    const out = await post(form.dataset.write, { key: form.elements.key.value, action: form.elements.action.value, kind, reason: form.elements.reason.value, items, seen });
    d.close();
    showResult(describe(out), out.group || out.decisions[0]);
    reloadParts();
  } catch (err) {
    $(form, "[data-hint]").textContent = refusal(err);
  } finally {
    busy(go, false);
  }
});

document.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-undo]");
  if (!b || !b.dataset.target) return;
  busy(b, true);
  try {
    const out = await post(`/api/decisions/${encodeURIComponent(b.dataset.target)}/undo`, { key: crypto.randomUUID(), reason: "undo right after deciding" });
    showResult(describe(out));
    reloadParts();
  } catch (err) {
    showResult(refusal(err));
  } finally {
    busy(b, false);
  }
});
