// registry.coffee: the outbox, the shot form, undo, sign-in and the setup forms. No build step.

// Times are shown in the phone's time zone; the server reads it from this cookie.
document.cookie = `tz=${encodeURIComponent(Intl.DateTimeFormat().resolvedOptions().timeZone)}; path=/; max-age=31536000; samesite=lax${location.protocol === "https:" ? "; secure" : ""}`;

// --- the outbox --------------------------------------------------------------------------------------
// Every write waits in IndexedDB until the server acknowledges it, keyed by the id the phone generated, so a
// dropped connection never loses a shot and a retry never makes a second one. Each entry carries the id of the
// person who made it, and only that person's entries are sent, so one phone shared by two people never sends a
// shot under the wrong account.

const ME = document.querySelector('meta[name="user"]')?.content ?? null;

const DB = new Promise((resolve, reject) => {
  const r = indexedDB.open("registry-coffee", 1);
  r.onupgradeneeded = () => r.result.createObjectStore("outbox", { keyPath: "key" });
  r.onsuccess = () => resolve(r.result);
  r.onerror = () => reject(r.error);
});

function store(mode, fn) {
  return DB.then((db) => new Promise((resolve, reject) => {
    const t = db.transaction("outbox", mode);
    const req = fn(t.objectStore("outbox"));
    t.oncomplete = () => resolve(req.result);
    t.onerror = () => reject(t.error);
  }));
}

class Rejected extends Error {} // the server said no (a 4xx other than 401/403/408/429): retrying won't help
class Held extends Error {} // the server refused for now (403): the entry stays on the phone and is sent again
const why = (err) => (err instanceof Rejected || err instanceof Held ? err.message : OFFLINE);

// Only a 2xx removes an entry. No connection, 401 (sign in again), 408, 429 and 5xx keep it and retry later; any
// other 4xx moves it to the "Couldn't save" list on screen, so a write is never dropped without the person seeing.
const RETRY = new Set([401, 403, 408, 429]);
let retryAt = 0; // when the next automatic flush is due (ms), from Retry-After or a doubling backoff
let backoff = 0;

function retryAfter(r) {
  const h = r.headers.get("retry-after");
  if (!h) return null;
  const s = Number(h);
  return Number.isFinite(s) ? s * 1000 : Math.max(0, Date.parse(h) - Date.now()) || null;
}

async function deliver(entry) {
  const r = await fetch(entry.url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Requested-With": "fetch" },
    body: JSON.stringify(entry.body),
  });
  if (r.ok) {
    await store("readwrite", (s) => s.delete(entry.key));
    return r.json();
  }
  const detail = (await r.json().catch(() => ({}))).detail;
  const reason = Array.isArray(detail) ? detail.join(" ") : detail || r.statusText;
  if (RETRY.has(r.status) || r.status >= 500) {
    // 403 is the origin check, which a proxy can trip by mistake: kept, and the person is told why.
    const err = r.status === 403 ? new Held("The server refused this for now. It’s kept on this phone and sent again later.") : new Error(reason);
    err.wait = retryAfter(r);
    throw err;
  }
  // Refused for good: kept, marked, and shown, never silently dropped.
  await store("readwrite", (s) => s.put({ ...entry, failed: reason || `Error ${r.status}` }));
  throw new Rejected(reason);
}

async function send(key, url, body) {
  const entry = { key, url, body, at: Date.now(), user: ME };
  await store("readwrite", (s) => s.put(entry));
  try {
    return await deliver(entry);
  } finally {
    showSync();
  }
}

async function mine() {
  return (await store("readonly", (s) => s.getAll())).filter((e) => ME && (e.user ?? ME) === ME);
}

function later(wait) {
  backoff = Math.min(backoff ? backoff * 2 : 2000, 5 * 60 * 1000);
  const delay = wait ?? backoff;
  retryAt = Date.now() + delay;
  setTimeout(() => { if (Date.now() >= retryAt) flush(); }, delay);
}

async function flush() {
  if (!ME) return;
  // In the order they were made: keys sort "retract:" before "shot:", and an undo must follow its shot. A retry
  // stops the queue, so nothing passes an earlier write that hasn't landed.
  const entries = (await mine()).filter((e) => !e.failed).sort((a, b) => a.at - b.at);
  for (const entry of entries) {
    try {
      await deliver(entry);
      backoff = 0;
    } catch (e) {
      if (!(e instanceof Rejected)) {
        later(e.wait);
        break;
      }
    }
  }
  showSync();
}

async function showSync() {
  const all = await mine();
  const n = all.filter((e) => !e.failed).length;
  for (const el of document.querySelectorAll("[data-sync]")) {
    if (n) el.textContent = `${n} waiting to sync`;
  }
  showFailed(all.filter((e) => e.failed));
}

const KIND = { shot: "A shot", retract: "Undoing a shot", bag: "A bag", equipment: "Equipment" };

function showFailed(failed) {
  const box = document.querySelector("[data-failed]");
  if (!box) return;
  box.hidden = !failed.length;
  const list = box.querySelector("ul");
  list.replaceChildren(...failed.map((e) => {
    const li = document.createElement("li");
    const when = new Date(e.at).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
    li.append(`${KIND[e.key.split(":")[0]] || "A change"} from ${when}: ${e.failed} `);
    const b = Object.assign(document.createElement("button"), { type: "button", className: "btn text", textContent: "Dismiss" });
    b.onclick = async () => { await store("readwrite", (s) => s.delete(e.key)); showSync(); };
    li.append(b);
    return li;
  }));
}

function uuid() {
  if (crypto.randomUUID) return crypto.randomUUID();
  const b = crypto.getRandomValues(new Uint8Array(16)); // plain http on a LAN has no randomUUID
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const h = [...b].map((x) => x.toString(16).padStart(2, "0")).join("");
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

const tick = (ms = 8) => navigator.vibrate?.(ms);
const OFFLINE = "Saved on this phone. It’ll sync when you’re back online.";

function hint(form, text) {
  form.querySelector("[data-hint]").textContent = text;
}

// A button with data-confirm asks for a second tap within 4 s: the first shows the confirm text in place of its
// label. Without data-confirm, the action runs at once.
function confirmTap(b, action) {
  const restore = () => {
    if (b.dataset.label === undefined) return;
    b.textContent = b.dataset.label;
    delete b.dataset.label;
  };
  if (!b.dataset.confirm || b.dataset.label !== undefined) {
    restore();
    return action();
  }
  b.dataset.label = b.textContent;
  b.textContent = b.dataset.confirm;
  setTimeout(restore, 4000);
}

function pressOne(group, button) {
  for (const b of group.querySelectorAll("button")) b.setAttribute("aria-pressed", String(b === button));
}

function pressed(group) {
  return group.querySelector('[aria-pressed="true"]')?.dataset.v ?? null;
}

// --- Log shot ----------------------------------------------------------------------------------------

function logShot(form) {
  const p = JSON.parse(document.getElementById("prefill").textContent);
  const $ = (id) => document.getElementById(id);
  const num = (el) => (el.value.trim() === "" ? null : parseFloat(el.value.replace(",", ".")));
  const sized = CSS.supports("field-sizing", "content");
  const fit = (el) => { if (!sized) el.style.width = `${Math.max(el.value.length, 2)}ch`; };
  // Dose and yield always show one decimal; grind shows its step's decimals only when it needs them.
  const show = (el, v) => {
    const digits = (String(el.dataset.step).split(".")[1] || "").length;
    el.value = v == null ? "" : el.dataset.digits ? Number(v).toFixed(el.dataset.digits) : String(Number(Number(v).toFixed(digits)));
    fit(el);
  };
  const taste = form.querySelector("[data-taste]");
  const rating = form.querySelector("[data-rating]");
  const save = form.querySelector("[data-save]");
  const timerBtn = form.querySelector("[data-timer]");
  let started = null;
  let ticker = null;
  let confirmLastTime = false;

  function update() {
    const d = num($("dose_g"));
    const y = num($("yield_g"));
    document.querySelector("[data-ratio]").textContent = d && y ? `1:${(y / d).toFixed(2)}` : "";
    if (confirmLastTime) {
      confirmLastTime = false;
      save.textContent = "Save shot";
    }
  }

  function stepBy(el, dir) {
    const step = Number(el.dataset.step);
    const min = Number(el.dataset.min);
    const v = (num(el) ?? min) + step * dir;
    show(el, Math.min(Number(el.dataset.max), Math.max(min, min + Math.round((v - min) / step) * step)));
    tick();
    update();
  }

  // One step on press, then repeat after 400 ms, every 80 ms, while held.
  for (const b of form.querySelectorAll("[data-for]")) {
    const el = $(b.dataset.for);
    let hold = null;
    const stop = () => { clearTimeout(hold); clearInterval(hold); };
    b.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      stepBy(el, Number(b.dataset.dir));
      hold = setTimeout(() => { hold = setInterval(() => stepBy(el, Number(b.dataset.dir)), 80); }, 400);
    });
    for (const ev of ["pointerup", "pointerleave", "pointercancel"]) b.addEventListener(ev, stop);
    b.addEventListener("click", (e) => { if (e.detail === 0) stepBy(el, Number(b.dataset.dir)); }); // keyboard
  }
  for (const id of ["dose_g", "yield_g", "grinder_setting", "time_s"]) {
    $(id).addEventListener("input", () => { fit($(id)); update(); });
    $(id).addEventListener("focus", () => $(id).select());
  }

  function timed() {
    form.querySelector("[data-timed]").hidden = false;
    form.querySelector("[data-untimed]").hidden = true;
  }

  function stopTimer() {
    clearInterval(ticker);
    show($("time_s"), Math.round((performance.now() - started) / 1000));
    started = null;
    timerBtn.textContent = "Start";
    timerBtn.classList.remove("running");
  }

  timerBtn.addEventListener("click", () => {
    tick(12);
    if (started !== null) return stopTimer();
    started = performance.now();
    timed();
    show($("time_s"), 0);
    // The numeral ticks once a second, and isn't animated.
    ticker = setInterval(() => show($("time_s"), Math.floor((performance.now() - started) / 1000)), 200);
    timerBtn.textContent = "Stop";
    timerBtn.classList.add("running");
    update();
  });
  form.querySelector("[data-type]").addEventListener("click", () => {
    if (started !== null) stopTimer();
    timed();
    $("time_s").focus();
  });

  taste.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    pressOne(taste, b.getAttribute("aria-pressed") === "true" ? null : b); // taste can be left empty
    sameShown();
    tick();
  });
  rating.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    pressOne(rating, b);
    sameShown();
    hint(form, "");
    tick();
  });
  // The chip shows as pressed only while taste and rating are exactly the last shot's.
  const sameChip = form.querySelector("[data-same]");
  function sameShown() {
    if (!sameChip) return;
    const last = p.same_as_last;
    const on = pressed(taste) === (last.taste ?? null) && pressed(rating) === String(last.rating);
    sameChip.setAttribute("aria-pressed", String(on));
  }
  sameChip?.addEventListener("click", () => {
    const last = p.same_as_last;
    pressOne(taste, last.taste ? taste.querySelector(`[data-v="${last.taste}"]`) : null);
    pressOne(rating, rating.querySelector(`[data-v="${last.rating}"]`));
    sameShown();
    hint(form, "");
    tick();
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (started !== null) stopTimer();
    const r = pressed(rating);
    if (!r) return hint(form, "Pick a rating to save.");
    let time = form.querySelector("[data-timed]").hidden ? null : num($("time_s"));
    if (time === null) {
      if (p.last_time_s == null) return hint(form, "Time the shot, or type the time.");
      if (!confirmLastTime) {
        confirmLastTime = true; // a forgotten timer costs one more tap, never a made-up time
        save.textContent = `Save with ${p.last_time_s} s?`;
        return;
      }
      time = p.last_time_s;
    }
    const body = {
      shot_id: uuid(),
      brewed_at: new Date().toISOString(),
      bag_id: p.bag_id,
      coffee_id: p.coffee_id,
      coffee_freetext: p.coffee_freetext,
      grinder_id: p.grinder_id,
      basket_id: p.basket_id,
      machine_id: p.machine_id,
      dose_g: num($("dose_g")),
      yield_g: num($("yield_g")),
      time_s: time,
      grinder_setting: num($("grinder_setting")),
      rating: Number(r),
      taste: pressed(taste),
      water_temp_c: p.water_temp_c,
      brew_method: p.brew_method,
      is_milk_drink: p.is_milk_drink,
    };
    for (const k of Object.keys(body)) if (body[k] == null) delete body[k];
    save.disabled = true;
    try {
      await send(`shot:${body.shot_id}`, "/api/shots", body);
      tick(8);
      setTimeout(() => tick(8), 60);
      location.href = `/saved/${body.shot_id}?new=1`;
    } catch (err) {
      hint(form, why(err));
      save.disabled = false;
    }
  });

  show($("dose_g"), p.dose_g);
  show($("yield_g"), p.yield_g);
  show($("grinder_setting"), p.grinder_setting);
  fit($("time_s"));
  update();
}

// --- Saved: undo for 5 s -----------------------------------------------------------------------------

function undo(strip) {
  const id = strip.dataset.undo;
  // Hidden after 5 s by a timer, not the animation: with reduced motion the bar doesn't move, and Undo still shows.
  // Focus on Undo holds it open.
  let timer = null;
  const arm = () => { timer = setTimeout(() => { strip.hidden = true; }, 5000); };
  strip.addEventListener("focusin", () => clearTimeout(timer));
  strip.addEventListener("focusout", arm);
  arm();
  strip.querySelector("button").addEventListener("click", async () => {
    try {
      await send(`retract:${id}`, `/api/shots/${id}/retract`, {});
      location.href = "/";
    } catch (err) {
      strip.firstChild.textContent = why(err);
    }
  });
}

// --- New bag and equipment ---------------------------------------------------------------------------

function newBag(form) {
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const coffee = form.coffee_freetext.value.trim();
    if (!coffee) return hint(form, "Type the roaster and coffee.");
    const body = { bag_id: uuid(), coffee_freetext: coffee };
    if (form.roast_date.value) body.roast_date = form.roast_date.value;
    try {
      await send(`bag:${body.bag_id}`, "/api/bags", body);
      location.href = "/log";
    } catch (err) {
      hint(form, why(err));
    }
  });
}

function equipment(form) {
  const choices = form.querySelectorAll("[data-choice]");
  for (const choice of choices) {
    choice.addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (b) pressOne(choice, b);
    });
  }
  // The whole record goes each time: a change is a new record under the same id, latest wins.
  async function save(extra) {
    const body = { equipment_id: form.dataset.id || uuid(), kind: form.dataset.equipment, ...extra };
    for (const el of form.querySelectorAll("input")) {
      el.removeAttribute("aria-invalid");
      const v = el.value.trim();
      if (!v) {
        if (!el.required) { body[el.name] = null; continue; } // cleared on purpose; fields not on the form are kept
        el.setAttribute("aria-invalid", "true");
        el.focus();
        return hint(form, "Fill in the fields that aren’t optional.");
      }
      body[el.name] = el.inputMode === "decimal" ? Number(v.replace(",", ".")) : v;
    }
    for (const choice of choices) body[choice.dataset.choice] = pressed(choice) === "true";
    try {
      await send(`equipment:${body.equipment_id}`, "/api/equipment", body);
      location.href = form.dataset.next || "/setup";
    } catch (err) {
      hint(form, why(err));
    }
  }
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    save({});
  });
  const retire = form.querySelector("[data-retire]");
  retire?.addEventListener("click", () => confirmTap(retire, () => save({ retired: true }))); // it leaves the pickers
}

// --- Joining and inviting -----------------------------------------------------------------------------

// The invite link carries its code after #, which never reaches the server; fill it in and say if it's good.
async function joinCode(form) {
  const input = form.querySelector("#code");
  const status = form.querySelector("[data-code-status]");
  const check = async () => {
    if (!input.value.trim()) return;
    try {
      const r = await post("/api/invites/check", { code: input.value });
      status.textContent = r.kind === "join" ? `Valid until ${r.expires}.` : "That’s a reset code. Use it on the reset screen.";
      input.removeAttribute("aria-invalid");
    } catch (err) {
      status.textContent = err.message;
      input.setAttribute("aria-invalid", "true");
    }
  };
  let fromLink = "";
  try {
    fromLink = decodeURIComponent(location.hash.slice(1));
  } catch {
    // A link that was cut or mangled in the message: say so, and keep it out of history.
    history.replaceState(null, "", location.pathname);
    status.textContent = "That link didn’t come through whole. Type the code from the message.";
    input.setAttribute("aria-invalid", "true");
  }
  if (fromLink) {
    input.value = fromLink;
    history.replaceState(null, "", location.pathname); // keep the code out of history and screenshots
    check();
  }
  input.addEventListener("change", check);
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false; // no clipboard API, or not allowed: the code stays on screen to copy by hand
  }
}

function invites() {
  const box = document.querySelector("[data-codebox]");
  const primary = document.querySelector("[data-new-invite]");
  const another = document.querySelector("[data-another]");
  const intro = document.querySelector("[data-code-intro]");
  const say = (text) => hint(document.querySelector("main"), text);
  const show = (r, name) => {
    const link = `${location.origin}/join#${r.code}`;
    const join = r.kind === "join";
    const groups = r.code.split("-");
    const [a, b] = box.querySelectorAll("[data-code] span");
    a.textContent = `${groups[0]}-${groups[1]}`; // two groups per line, never broken inside one
    b.textContent = `${groups[2]}-${groups[3]}`;
    box.querySelector("[data-code-label]").textContent = join ? "Invite code" : `Reset code for ${name}`;
    box.querySelector("[data-code-note]").textContent = join
      ? `${location.host}/join · until ${r.expires}`
      : `Give ${name} this code. It works once, until ${r.expires}, and signs them out everywhere.`;
    intro.textContent = join
      ? `Share the link. It works once and expires ${r.expires}.`
      : `Give ${name} the code below.`;
    box.hidden = false;
    // The one filled button now sends this code; making another one is a text button with a second tap.
    const share = join && navigator.share;
    primary.textContent = share ? "Share link" : join ? "Copy link" : "Copy code";
    primary.onclick = async () => {
      if (share) return navigator.share({ title: "registry.coffee", url: link }).catch(() => {});
      say((await copyText(join ? link : r.code)) ? "" : "Couldn’t copy. Press and hold the code to copy it.");
      if (!share) primary.textContent = "Copied";
    };
    another.hidden = !join;
    box.scrollIntoView({ block: "center" });
  };
  const act = async (url, body, after) => {
    try {
      after(await post(url, body));
    } catch (err) {
      say(err.message);
    }
  };
  const makeOne = () => act("/api/invites", {}, (r) => show(r));
  primary.onclick = makeOne;
  another.onclick = () => confirmTap(another, makeOne);
  for (const b of document.querySelectorAll("[data-reset]")) {
    b.onclick = () => act("/api/invites", { kind: "reset", for_user_id: b.dataset.reset }, (r) => show(r, b.dataset.name));
  }
  for (const b of document.querySelectorAll("[data-renew]")) {
    b.onclick = () => act(`/api/invites/${b.dataset.renew}/renew`, {}, (r) => { b.previousElementSibling.hidden = true; b.nextElementSibling.hidden = true; b.hidden = true; show(r); });
  }
  for (const b of document.querySelectorAll("[data-revoke]")) {
    b.onclick = () => act(`/api/invites/${b.dataset.revoke}/revoke`, {}, () => location.reload());
  }
  for (const b of document.querySelectorAll("[data-person]")) {
    b.onclick = () => confirmTap(b, () => act(`/api/people/${b.dataset.person}`, { disabled: b.dataset.disabled === "true" }, () => location.reload()));
  }
}

// The theme lives in a cookie so the server renders the right one; no flash of the other.
function themeChoice(group) {
  group.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    pressOne(group, b);
    const v = b.dataset.v;
    document.cookie = `theme=${v}; path=/; max-age=31536000; samesite=lax${location.protocol === "https:" ? "; secure" : ""}`;
    if (v === "auto") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.dataset.theme = v;
  });
}

// --- Signing in and the account -----------------------------------------------------------------------

async function post(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Requested-With": "fetch" },
    body: JSON.stringify(body),
  });
  if (r.ok) return r.json();
  const detail = (await r.json().catch(() => ({}))).detail;
  throw new Rejected(Array.isArray(detail) ? detail.join(" ") : detail || "Something went wrong. Try again.");
}

function showPassword(button) {
  const input = document.getElementById(button.getAttribute("aria-controls"));
  button.addEventListener("click", () => {
    const show = input.type === "password";
    input.type = show ? "text" : "password";
    button.textContent = show ? "Hide" : "Show";
    button.setAttribute("aria-pressed", String(show));
  });
}

function signIn(form) {
  const submit = form.querySelector('[type="submit"]');
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = {};
    for (const el of form.querySelectorAll("input")) body[el.name] = el.value;
    for (const el of form.querySelectorAll("input")) el.removeAttribute("aria-invalid");
    const empty = [...form.querySelectorAll("input")].find((el) => !el.value.trim());
    if (empty) {
      empty.setAttribute("aria-invalid", "true");
      empty.focus();
      return hint(form, "Fill in every field.");
    }
    submit.disabled = true;
    try {
      await post(form.dataset.api || "/api/signin", body);
      location.replace("/");
    } catch (err) {
      hint(form, err instanceof Rejected ? err.message : "Couldn’t reach the server. Check the connection.");
      if (err instanceof Rejected) {
        for (const el of form.querySelectorAll("input")) {
          el.setAttribute("aria-invalid", "true");
          el.setAttribute("aria-errormessage", "signin-hint");
        }
      }
      submit.disabled = false;
    }
  });
}

function account() {
  const form = document.getElementById("password-form");
  form?.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await post("/api/password", { current: form.current.value, new: form.new.value });
      form.reset();
      hint(form, "");
      form.closest("details").open = false;
    } catch (err) {
      hint(form, err instanceof Rejected ? err.message : "Couldn’t reach the server. Check the connection.");
    }
  });
  for (const b of document.querySelectorAll("[data-signout]")) {
    // Signing out everywhere asks for a second tap (data-confirm); plain sign-out doesn't.
    b.addEventListener("click", () => confirmTap(b, async () => {
      try {
        await post(b.dataset.signout, {});
        location.replace("/signin"); // only once the server has signed this phone (or every phone) out
      } catch {
        b.textContent = "Couldn’t sign out. Check the connection and try again.";
      }
    }));
  }
}

document.addEventListener("DOMContentLoaded", () => {
  const shot = document.getElementById("shot");
  if (shot) logShot(shot);
  const strip = document.querySelector("[data-undo]");
  if (strip) undo(strip);
  const bag = document.getElementById("bag");
  if (bag) newBag(bag);
  for (const f of document.querySelectorAll("[data-equipment]")) equipment(f);
  const theme = document.querySelector("[data-theme-choice]");
  if (theme) themeChoice(theme);
  for (const id of ["signin", "join", "reset"]) {
    const f = document.getElementById(id);
    if (f) signIn(f);
  }
  const join = document.getElementById("join");
  if (join) joinCode(join);
  if (document.querySelector("[data-new-invite]")) invites();
  for (const b of document.querySelectorAll("[data-show]")) showPassword(b);
  // A date input shows "mm/dd/yyyy" in full ink while empty; mark it so it reads as a placeholder.
  for (const el of document.querySelectorAll("[data-empty-class]")) {
    const mark = () => el.classList.toggle("empty", !el.value);
    el.addEventListener("input", mark);
    mark();
  }
  account();
  flush();
});
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
addEventListener("online", flush);
