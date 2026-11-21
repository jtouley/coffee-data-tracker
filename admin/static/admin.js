// admin.registry.coffee: loads each page's data region (with its loading, slow and error states), submits facet
// filters, and runs the WebAuthn ceremonies for sign-in. No inline script anywhere (CSP script-src 'self').
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
  if (!r.ok) throw Object.assign(new Error(data.detail || `The server refused (${r.status}).`), { status: r.status });
  return data;
}

// --- data regions -------------------------------------------------------------------------------------------

function loadPart(section) {
  const url = section.dataset.part;
  const loading = section.dataset.loadingHtml || section.innerHTML;
  section.dataset.loadingHtml = loading;
  section.innerHTML = loading;
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
      section.innerHTML = await r.text(); // server-rendered and escaped by the template engine
      // Filters start open where there's room for them beside the results; on a phone they fold away.
      if (matchMedia("(min-width: 760px)").matches) section.querySelectorAll("[data-filters]").forEach((d) => { d.open = true; });
    })
    .catch(() => {
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
