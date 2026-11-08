// registry.coffee: the outbox, the shot form, undo, sign-in and the setup forms. No build step.

// Times are shown in the phone's time zone; the server reads it from this cookie.
document.cookie = `tz=${encodeURIComponent(Intl.DateTimeFormat().resolvedOptions().timeZone)}; path=/; max-age=31536000; samesite=lax${location.protocol === "https:" ? "; secure" : ""}`;

// --- the outbox --------------------------------------------------------------------------------------
// Every write waits in IndexedDB until the server acknowledges it, keyed by the id the phone generated, so a
// dropped connection never loses a shot and a retry never makes a second one. Each entry carries the id of the
// person who made it, and only that person's entries are sent, so one phone shared by two people never sends a
// shot under the wrong account.

const ME = document.querySelector('meta[name="user"]')?.content ?? null;

// Cached Today and Log pages contain a specific bag. Never reuse them after a selection changes.
async function clearBagPages() {
  if (!("caches" in window)) return;
  const cache = await caches.open("pages");
  await Promise.all([cache.delete("/"), cache.delete("/log")]);
}
function selectedBag() {
  try {
    const value = localStorage.getItem(`selected-bag:${ME}`);
    if (!value) return null;
    try { return JSON.parse(value); } catch { return { id: value, at: 0 }; }
  } catch { return null; }
}
function rememberBag(id, at = Date.now()) {
  try { localStorage.setItem(`selected-bag:${ME}`, JSON.stringify({ id, at })); } catch { /* storage is optional */ }
}
// Choosing a bag is remembered on this phone only once the server has accepted it: a failed /use (offline, a bag
// finished elsewhere) leaves the old value, and the server's selection is what Log shot uses either way.
document.addEventListener("click", async (event) => {
  const link = event.target.closest("[data-select-bag]");
  if (!link || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  let r = null;
  try { r = await fetch(link.href, { headers: { "X-Requested-With": "fetch" } }); } catch { /* offline: navigate below */ }
  if (!r || !r.ok) return location.assign(link.href);
  rememberBag(link.dataset.selectBag);
  await clearBagPages().catch(() => {});
  location.assign(r.url);
});

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

const sending = new Map(); // key -> the delivery in flight, so Undo can wait for it instead of racing it
const refused = new Map(); // key -> why the server refused it (4xx), so a view doesn't then say "Synced"

function deliver(entry) {
  // One delivery per entry at a time: a direct send and a flush on reconnect can reach the same entry together.
  if (sending.has(entry.key)) return sending.get(entry.key);
  const p = deliverOnce(entry).finally(() => sending.delete(entry.key));
  sending.set(entry.key, p);
  return p;
}

// A write that landed makes the pages shown before it out of date: a page the browser brings back from history
// (Back) loads again if it was made before the write (stale(), below). The time is the server's (its Date header),
// as the page's own (meta "rendered"), so the phone's clock doesn't matter. The pages kept for offline use stay:
// online, the service worker fetches pages from the network first, so they're only shown offline.
function wrote(r) {
  const at = Date.parse(r.headers.get("date") || "") || Date.now();
  try { localStorage.setItem("wrote-at", String(at)); } catch { /* storage is optional */ }
}

async function deliverOnce(entry) {
  // A photo waits as a Blob (IndexedDB keeps those); everything else is JSON.
  // X-Expected-User binds the write to the account that made it: after an account switch (another tab, a sign-out)
  // the server answers 403 and the entry is held until that person signs in again. An entry from before entries
  // carried an account goes as the signed-in person, as it always did (mine() already counts it as theirs).
  const r = await fetch(entry.url, {
    method: "POST",
    headers: { "Content-Type": entry.blob ? entry.blob.type || "image/jpeg" : "application/json", "X-Requested-With": "fetch", "X-Expected-User": entry.user ?? ME },
    body: entry.blob || JSON.stringify(entry.body),
  });
  if (r.ok) {
    wrote(r);
    if (entry.url === "/api/bags" && entry.body?.finished_on) {
      try {
        if (selectedBag()?.id === entry.body.bag_id) {
          localStorage.removeItem(`selected-bag:${ME}`);
          await clearBagPages();
        }
      } catch { /* storage is optional */ }
    }
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
  refused.set(entry.key, reason);
  throw new Rejected(reason);
}

async function send(key, url, body, blob = null) {
  const entry = { key, url, body, blob, at: Date.now(), user: ME };
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


let flushing = null;
function flush() {
  // One pass at a time: "online" and a page load can both start one, and two would send each entry twice.
  flushing ??= flushOnce().finally(() => { flushing = null; });
  return flushing;
}

async function flushOnce() {
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

const SYNCED = '<svg viewBox="0 0 12 12" width="12" height="12" aria-hidden="true"><path d="M2 6.5l2.5 2.5L10 3" fill="none" stroke="currentColor" stroke-width="1.75"/></svg>Synced';

async function showSync() {
  const all = await mine();
  const n = all.filter((e) => !e.failed).length;
  for (const el of document.querySelectorAll("[data-sync]")) {
    if (n) el.textContent = `${n} waiting to sync`;
    else if ("quiet" in el.dataset) el.textContent = ""; // where nothing was waiting, nothing to say
    else el.innerHTML = SYNCED;
  }
  if (!n) document.dispatchEvent(new Event("synced"));
  showFailed(all.filter((e) => e.failed));
}

const KIND = { shot: "A shot", retract: "Undoing a shot", restore: "Bringing a shot back", bag: "A bag", photo: "A bag photo", equipment: "Equipment" };

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

// After a save: say what happened on the screen it lands on, and take the form out of the way, so Back doesn't
// reopen what was just saved (and a second Save make a second record). Going back to the screen it was opened from
// steps back in history; anywhere else replaces the form. The notice waits in sessionStorage, which works offline.
function go(url, notice) {
  try {
    if (notice) sessionStorage.setItem("notice", notice);
  } catch {
    // storage blocked: the move still happens, without the notice
  }
  const here = location.pathname + location.search;
  const from = document.referrer ? new URL(document.referrer) : null;
  const opener = from && from.origin === location.origin && from.pathname + from.search;
  // "/shot/ID" is also the "/shot/ID?from=today" it was opened as: step back to that, keeping its back link.
  const same = opener === url || (from && !url.includes("?") && from.origin === location.origin && from.pathname === url);
  if (url !== here.split("?")[0] && url !== here && same && history.length > 1) history.back();
  else location.replace(url); // also this same screen again, after a change on it
}

// Stepping back to a screen, the browser shows its copy from before (from memory, or from its cache, which Back
// doesn't check): if a save is waiting to be announced on it, or any write landed after the copy was made (a shot
// moved to another bag, from a screen further on), load it again, so it shows the change, and the notice with it.
function stale(persisted) {
  try {
    const back = persisted || performance.getEntriesByType("navigation")[0]?.type === "back_forward";
    if (!back) return false;
    if (!navigator.onLine) return false; // offline, a reload would only find "No connection": keep what's shown
    if (sessionStorage.getItem("notice")) return true;
    const at = Number(localStorage.getItem("wrote-at"));
    const made = Number(document.querySelector('meta[name="rendered"]')?.content) * 1000;
    // Both in whole seconds, and a server's Date header can lag its clock by a second (uvicorn's is set once a
    // second), so a page made up to 2 s after the write is loaded again too: one reload too many, never one too few.
    return Boolean(at && made && at + 2000 >= made);
  } catch {
    return false; // storage blocked: nothing waiting
  }
}
addEventListener("pageshow", (e) => { if (e.persisted && stale(true)) location.reload(); });

function notify(text) {
  const el = document.querySelector("[data-notice]");
  if (!el) return;
  // Shown empty, then filled a moment later: a live region that appears with its text already in it (or changes
  // as the page loads) is often not read out.
  el.textContent = "";
  el.hidden = !text;
  if (text) setTimeout(() => { el.textContent = text; }, 100);
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
  const saveLabel = save.textContent;
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
      save.textContent = saveLabel; // "Save shot" or "Save correction"
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

  // Correcting a past shot has steppers for the time instead of the timer.
  if (timerBtn) {
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
  }

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
    // Correcting, the fallback is the corrected shot's own time, not the bag's latest (#18-1).
    const lastTime = p.correct ? p.correct.time_s : p.last_time_s;
    if (time === null) {
      if (lastTime == null) return hint(form, "Time the shot, or type the time.");
      if (!confirmLastTime) {
        confirmLastTime = true; // a forgotten timer costs one more tap, never a made-up time
        save.textContent = `Save with ${lastTime} s?`;
        return;
      }
      time = lastTime;
    }
    const fix = p.correct; // correcting: same id and time, a new record that replaces the old one
    const temp = num($("water_temp_c"));
    const body = {
      shot_id: fix ? fix.shot_id : uuid(),
      brewed_at: fix ? fix.brewed_at : new Date().toISOString(),
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
      water_temp_c: temp,
      brew_method: p.brew_method,
      is_milk_drink: pressed(milk) === "true",
      notes: $("notes").value.trim() || null,
    };
    for (const k of Object.keys(body)) if (body[k] == null) delete body[k];
    const label = save.textContent;
    save.disabled = true;
    save.textContent = "Saving…"; // the lake commit can take seconds: the tap shows at once
    try {
      await send(`shot:${body.shot_id}`, "/api/shots", body);
      tick(8);
      setTimeout(() => tick(8), 60);
      if (fix) go(`/shot/${body.shot_id}`, "Correction saved");
      else location.replace(`/saved/${body.shot_id}?new=1`); // Saved is the confirmation, with its Undo
    } catch (err) {
      if (err instanceof Rejected) {
        hint(form, err.message);
        save.disabled = false;
        save.textContent = label;
        return;
      }
      tick(8);
      setTimeout(() => tick(8), 60);
      offlineSaved(body); // it's in the outbox: say so, and don't leave the form armed for a second save
    }
  });

  // Saved with no connection: what was saved, described from the form alone (no server logic copied here).
  function offlineSaved(b) {
    const view = document.querySelector("[data-offline-saved]");
    const o = (k) => view.querySelector(`[data-o="${k}"]`);
    const now = new Date();
    o("title").textContent = `Saved ${now.getHours()}:${String(now.getMinutes()).padStart(2, "0")} · ${view.dataset.name}`;
    o("ratio").textContent = `1:${(b.yield_g / b.dose_g).toFixed(2)}`;
    o("dose").textContent = `${b.dose_g.toFixed(1)} → ${b.yield_g.toFixed(1)}`;
    o("time").textContent = `${b.time_s ?? "–"}`;
    o("grind").textContent = `Grind ${b.grinder_setting} · ${view.dataset.grinder} · rated ${b.rating}`;
    // The same tag as Saved.
    const tag = o("taste");
    tag.className = `taste ${b.taste || "none"}`;
    tag.textContent = b.taste ? `${b.taste[0].toUpperCase()}${b.taste.slice(1)}` : "No taste";
    form.hidden = true;
    for (const el of document.querySelectorAll("[data-online-only]")) el.hidden = true;
    view.hidden = false;
    view.querySelector("h1").focus({ preventScroll: true });
    scrollTo(0, 0);
    // Once it syncs, the comparison is a tap away; if the server refused it, say why instead (#20-5).
    document.addEventListener("synced", () => {
      const no = refused.get(`shot:${b.shot_id}`);
      if (no) return void (o("summary").textContent = `The server didn’t take this shot: ${no}`);
      const link = Object.assign(document.createElement("a"), { className: "ext", href: p.correct ? `/shot/${b.shot_id}` : `/saved/${b.shot_id}`, textContent: "Synced. See how it compares ›" });
      o("summary").replaceChildren(link);
    }, { once: true });
    // A correction has no Undo, offline as online: undoing it once it synced would retract the shot (#20-4).
    const strip = view.querySelector("[data-unqueue]");
    if (p.correct) strip.hidden = true;
    else unqueue(strip, b.shot_id);
  }

  // More: water, milk and notes. Water and milk carry over from the last shot; notes start empty.
  const milk = form.querySelector("[data-milk]");
  const moreSummary = form.querySelector("[data-more-summary]");
  const note = () => {
    const t = $("notes").value.trim();
    return t.length > 24 ? `${t.slice(0, 23)}…` : t || null;
  };
  const summarize = () => {
    const parts = [num($("water_temp_c")) != null ? `${num($("water_temp_c"))} °C` : null, pressed(milk) === "true" ? "milk drink" : "no milk", note()];
    moreSummary.textContent = parts.filter(Boolean).join(" · ");
  };
  milk.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (b) { pressOne(milk, b); tick(); }
    summarize();
  });
  $("water_temp_c").value = p.water_temp_c ?? "";
  pressOne(milk, milk.querySelector(`[data-v="${p.is_milk_drink ? "true" : "false"}"]`));
  for (const id of ["water_temp_c", "notes"]) $(id).addEventListener("input", summarize);

  show($("dose_g"), p.dose_g);
  show($("yield_g"), p.yield_g);
  show($("grinder_setting"), p.grinder_setting);
  if (p.correct) {
    timed();
    show($("time_s"), p.correct.time_s);
    if (p.correct.taste) pressOne(taste, taste.querySelector(`[data-v="${p.correct.taste}"]`));
    pressOne(rating, rating.querySelector(`[data-v="${p.correct.rating}"]`));
    $("notes").value = p.correct.notes ?? "";
  }
  summarize();
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
      go("/", "Shot undone");
    } catch (err) {
      strip.firstChild.textContent = why(err);
    }
  });
}

// Undo for a shot that may still be on the phone: take it out of the outbox; if it already went up, retract it.
function unqueue(strip, id) {
  let timer = setTimeout(() => { strip.hidden = true; }, 5000);
  strip.addEventListener("focusin", () => clearTimeout(timer));
  strip.addEventListener("focusout", () => { timer = setTimeout(() => { strip.hidden = true; }, 5000); });
  strip.querySelector("button").addEventListener("click", async () => {
    clearTimeout(timer);
    const key = `shot:${id}`;
    await sending.get(key)?.catch(() => {}); // a send already under way finishes first; then it's retracted (#20-3)
    const queued = await store("readonly", (s) => s.get(key));
    try {
      if (queued) await store("readwrite", (s) => s.delete(key));
      else await send(`retract:${id}`, `/api/shots/${id}/retract`, {});
      go("/", "Shot undone");
    } catch (err) {
      strip.firstChild.textContent = why(err);
    }
  });
}

// --- New bag and equipment ---------------------------------------------------------------------------

// Days from a roast date to today on this phone.
function roastDays(iso) {
  const today = new Date();
  today.setHours(12, 0, 0, 0);
  return Math.round((today - new Date(`${iso}T12:00:00`)) / 86400000);
}

// A roast date typed with the wrong year is easy to miss ("day 3665"): one in the future or over a year ago isn't
// saved, and one more than 90 days old asks for a second tap on Save, which says how old it is. Returns what to say,
// or "" to save. `kept` is the date the bag already has: it's saved as it is, like the server does, so the weight or
// Finished can change on a bag whose date predates this check. "Future" and "over a year" are the server's rules,
// past tomorrow or before 366 days ago in UTC, so a phone ahead of UTC or behind it never offers a date the server
// then refuses.
function roastCheck(form, save, label, kept = null) {
  const v = form.roast_date.value;
  if (!v || v === kept) return "";
  if (v > new Date(Date.now() + 86400000).toISOString().slice(0, 10)) return "That roast date is in the future. Check the year.";
  if (v < new Date(Date.now() - 366 * 86400000).toISOString().slice(0, 10)) return "That roast date is over a year ago. Check the year.";
  const days = roastDays(v);
  if (days > 90 && save.textContent === label) {
    save.textContent = `Save with day ${days}?`; // like "Save with 29 s?" on Log shot
    return " "; // not an error: the button asks
  }
  return "";
}

function armRoastCheck(form, save) {
  const label = save.textContent;
  form.roast_date.addEventListener("input", () => { save.textContent = label; });
  return label;
}

// Edit a saved bag: the whole record again under the same id, so the change is a new record (latest wins) and a
// resend is harmless. Changing the coffee's name looks it up in the catalog again.
function bagEdit(form) {
  const rec = JSON.parse(document.getElementById("bag-record").textContent);
  const save = form.querySelector('[type="submit"]');
  const label = armRoastCheck(form, save);
  const search = form.querySelector("[data-search]");
  const query = form.querySelector("#catalog-query");
  const results = form.querySelector("[data-search-results]");
  const status = form.querySelector("[data-search-status]");
  const match = form.querySelector("[data-match]");
  let selected = null;
  let request = 0;
  form.querySelector("[data-search-open]").addEventListener("click", () => {
    search.hidden = false;
    query.value = form.coffee_freetext.value;
    query.focus();
  });
  query.addEventListener("input", () => { request++; results.replaceChildren(); status.textContent = ""; });
  query.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); form.querySelector("[data-search-submit]").click(); }
  });
  form.coffee_freetext.addEventListener("input", () => {
    selected = null;
    match.textContent = form.coffee_freetext.value.trim() === (rec.coffee_freetext || "").trim()
      ? (rec.coffee_id ? "The existing catalog match will be kept." : "Not matched to the catalog")
      : "The name will be matched when you save, unless you select a catalog result.";
  });
  form.querySelector("[data-search-submit]").addEventListener("click", async () => {
    const term = query.value.trim();
    const id = ++request;
    results.replaceChildren();
    if (term.length < 2) { status.textContent = "Type at least two characters."; return; }
    status.textContent = "Searching the catalog…";
    try {
      const response = await fetch(`/api/coffees/search?q=${encodeURIComponent(term)}`);
      if (response.status === 503) {
        if (id === request) status.textContent = "The catalog isn’t available right now. Try again shortly.";
        return;
      }
      if (!response.ok) throw new Error();
      const data = await response.json();
      if (id !== request) return;
      status.textContent = data.coffees.length ? "Choose the coffee on your bag." : "No coffees found. Try another search.";
      for (const coffee of data.coffees) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "pickrow";
        const name = document.createElement("span");
        name.className = "name";
        name.textContent = coffee.title;
        const detail = document.createElement("span");
        detail.className = "meta";
        detail.textContent = [coffee.roaster, coffee.origin, coffee.past ? "past offering" : coffee.in_stock == null ? "" : coffee.in_stock ? "in stock" : "sold out"].filter(Boolean).join(" · ");
        name.append(detail);
        button.append(name);
        button.addEventListener("click", () => {
          request++;
          selected = coffee;
          form.coffee_freetext.value = coffee.title;
          match.textContent = `Selected: ${[coffee.roaster, coffee.title].filter(Boolean).join(" · ")}. Save the bag to match it.`;
          search.hidden = true;
          results.replaceChildren();
          status.textContent = "";
        });
        results.append(button);
      }
    } catch {
      if (id === request) status.textContent = "Couldn’t search the catalog. Check your connection and try again.";
    }
  });
  const finished = form.querySelector("[data-finished]");
  finished.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (b) { pressOne(finished, b); tick(); }
  });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const coffee = form.coffee_freetext.value.trim();
    if (!coffee) return hint(form, "Type the roaster and coffee.");
    const said = roastCheck(form, save, label, rec.roast_date);
    if (said) return hint(form, said.trim());
    hint(form, "");
    const weight = form.weight_g.value.trim();
    if (weight && !(Number(weight) > 0)) return hint(form, "The weight is a number of grams, like 340.");
    const now = new Date();
    const today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
    const body = {
      ...rec,
      coffee_freetext: coffee,
      roast_date: form.roast_date.value || null,
      weight_g: weight ? Math.round(Number(weight)) : null,
      finished_on: pressed(finished) === "true" ? rec.finished_on || today : null,
    };
    if (selected) Object.assign(body, { coffee_id: selected.coffee_id, match_score: null });
    else if (coffee !== (rec.coffee_freetext || "")) Object.assign(body, { coffee_id: null, match_score: null });
    const shown = save.textContent;
    save.disabled = true;
    save.textContent = "Saving…"; // as on Log shot: the lake commit can take seconds
    try {
      await send(`bag:${rec.bag_id}`, "/api/bags", body);
      go(form.dataset.next, `Bag saved: ${coffee}`);
    } catch (err) {
      if (!(err instanceof Rejected)) return go(form.dataset.next, `Bag saved on this phone: ${coffee}. It’ll sync when you’re back online.`);
      hint(form, err.message);
      save.disabled = false;
      save.textContent = shown;
    }
  });
}

// New bag: a photo of the label, read by the server (Claude reads, Python matches), or the coffee typed.
function newBag(root) {
  const form = document.getElementById("bag");
  const input = document.getElementById("photo");
  const steps = root.querySelectorAll("[data-step]");
  const retakeTop = document.querySelector("[data-retake]");
  const step = (name) => {
    for (const s of steps) s.hidden = s.dataset.step !== name;
    if (retakeTop) retakeTop.hidden = !["match", "pick"].includes(name);
    scrollTo(0, 0);
    root.querySelector(`[data-step="${name}"] h1`)?.focus({ preventScroll: true }); // screen readers hear the step
  };
  let read = null; // what the label said: carried into the bag, whichever way it's chosen
  let attempt = 0; // which photo the screen is waiting on; replies for any other are dropped
  let saving = false;

  const save = async (body, where) => {
    if (saving) return; // a second tap mustn't make a second bag
    saving = true;
    for (const b of root.querySelectorAll("button")) b.disabled = true;
    body = { bag_id: uuid(), ...body };
    if (read) Object.assign(body, { photo_id: read.photo_id, read_json: read.raw });
    if (read?.pending) body.read_pending = true; // only then may the photo's match fill in the bag (#20-1)
    if (read?.roast_date && !body.roast_date && where !== form) body.roast_date = read.roast_date; // typed: as typed, even cleared
    if (read?.label?.weight_g && !body.weight_g) body.weight_g = read.label.weight_g;
    const name = body.coffee_freetext;
    try {
      await send(`bag:${body.bag_id}`, "/api/bags", body);
      // Saved online: it's the bag on the grinder now. A bag saved offline is picked with Switch once it syncs.
      try {
        if ((await fetch(`/bag/${encodeURIComponent(body.bag_id)}/use`)).ok) {
          rememberBag(body.bag_id);
          await clearBagPages();
        }
      } catch { /* the server's bag stays selected */ }
      go("/log", `Bag saved: ${name}`); // on to the first shot of it
    } catch (err) {
      // Queued: it's saved on the phone, so on to Today, saying so. The kept Log shot page still has the last
      // bag on it, so it isn't where to go until this one syncs.
      if (!(err instanceof Rejected)) return go("/", `Bag saved on this phone: ${name}. It’ll sync when you’re back online.`);
      hint(where, why(err));
      saving = false;
      for (const b of root.querySelectorAll("button")) b.disabled = false;
    }
  };
  const said = () => [read?.label?.roaster, read?.label?.coffee && `“${read.label.coffee}”`].filter(Boolean).join(", ");
  const typed = () => {
    if (read?.label) form.coffee_freetext.value = [read.label.roaster, read.label.coffee].filter(Boolean).join(" ");
    if (read?.roast_date) form.roast_date.value = read.roast_date;
    form.roast_date.dispatchEvent(new Event("input"));
    step("type");
  };
  const fill = (sel, text) => {
    const el = root.querySelector(`[data-f="${sel}"]`);
    el.textContent = text || "";
    if (el.tagName === "DD") el.hidden = el.previousElementSibling.hidden = !text;
  };
  const showMatch = (m) => {
    fill("title", m.title);
    fill("roaster", m.roaster);
    const d = read.roast_date;
    fill("roasted", d ? `${new Date(`${d}T12:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric" })} · day ${roastDays(d)} · read from the bag` : "");
    fill("origin", m.origin);
    fill("notes", m.notes);
    fill("price", m.price);
    fill("stock", m.past ? "Past offering" : m.in_stock == null ? "" : m.in_stock ? "In stock" : "Sold out");
    // A date read off the label more than 90 days back (a misread year) asks once, as a typed one does.
    const use = root.querySelector("[data-use]");
    use.textContent = "Use this coffee";
    const old = d && roastDays(d) > 90 ? roastDays(d) : 0;
    use.onclick = () => {
      if (old && use.textContent === "Use this coffee") return void (use.textContent = `Save with day ${old}?`);
      save({ coffee_id: m.coffee_id, coffee_freetext: m.title, match_score: m.score ?? (m.coffee_id === read?.match?.coffee_id ? read.score : null) }, root.querySelector('[data-step="match"]'));
    };
    step("match");
    root.querySelector('[data-goto="pick"]').hidden = !(read.candidates || []).some((c) => c.coffee_id !== m.coffee_id);
  };
  const chevron = () => {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 8 14");
    svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", "M1 1l6 6-6 6");
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", "currentColor");
    path.setAttribute("stroke-width", "1.75");
    svg.append(path);
    return svg;
  };
  // Candidates: tapping one shows its card to confirm, so a slip of the thumb doesn't save the wrong coffee.
  const showPick = (fromMatch = false) => {
    const list = root.querySelector("[data-candidates]");
    list.replaceChildren();
    const others = (read.candidates || []).filter((c) => !fromMatch || c.coffee_id !== read.match?.coffee_id);
    const tied = others.length > 1 && Math.abs((others[0].score ?? 0) - (others[1].score ?? 0)) < 5; // a score only helps when it differs
    for (const c of others) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "pickrow";
      const name = document.createElement("span");
      name.className = "name";
      name.textContent = c.title;
      const meta = document.createElement("span");
      meta.className = "meta";
      const sameRoaster = read.label?.roaster && c.roaster?.toLowerCase().includes(read.label.roaster.toLowerCase());
      meta.textContent = [sameRoaster ? "" : c.roaster, c.origin, c.past ? "past offering" : c.in_stock == null ? "" : c.in_stock ? "in stock" : "sold out"]
        .filter(Boolean).join(" · ");
      name.append(meta);
      const score = document.createElement("span");
      score.className = "meta";
      score.textContent = c.score && !tied ? `match ${c.score}` : "";
      b.append(name, score, chevron());
      b.onclick = () => showMatch(c);
      list.append(b);
    }
    const when = read.roast_date ? `, roasted ${new Date(`${read.roast_date}T12:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric" })}` : "";
    root.querySelector("[data-said]").textContent = fromMatch
      ? `Other coffees in the catalog that could be it.`
      : others.length
        ? `The bag says ${said()}${when}. These are the closest in the catalog.`
        : `The bag says ${said()}${when}, but nothing like it is in the catalog. Type it and it’s saved as written.`;
    const none = root.querySelector("[data-none]");
    none.className = others.length ? "btn text" : "btn primary";
    none.textContent = others.length ? "None of these: type the coffee" : "Type the coffee";
    step("pick");
  };
  // Retake leads only where a new photo can help; otherwise typing is the one filled button.
  const failed = (title, body, retake = true) => {
    fill("failed-title", title);
    fill("failed-body", body);
    const r = root.querySelector("[data-failed-retake]");
    const t = root.querySelector("[data-failed-type]");
    r.hidden = !retake;
    t.className = retake ? "btn text" : "btn primary";
    step("failed");
  };

  // The phone shrinks the photo to 1600 px and re-encodes it, which also drops its EXIF (location included).
  const shrink = async (file) => {
    try {
      const bmp = await createImageBitmap(file, { imageOrientation: "from-image" });
      const scale = Math.min(1, 1600 / Math.max(bmp.width, bmp.height));
      const canvas = document.createElement("canvas");
      canvas.width = Math.round(bmp.width * scale);
      canvas.height = Math.round(bmp.height * scale);
      canvas.getContext("2d").drawImage(bmp, 0, 0, canvas.width, canvas.height);
      return await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.85));
    } catch {
      return file; // the server re-encodes it anyway
    }
  };

  const take = () => input?.click();
  for (const b of root.querySelectorAll("[data-take]")) b.onclick = take;
  for (const b of root.querySelectorAll("[data-goto]")) {
    b.onclick = () => {
      if (b.dataset.goto === "type") attempt++; // typing instead: a read still on its way is dropped
      return b.dataset.goto === "type" ? typed() : b.dataset.goto === "pick" ? showPick(Boolean(read?.match)) : step(b.dataset.goto);
    };
  }
  input?.addEventListener("change", async () => {
    const file = input.files[0];
    input.value = "";
    if (!file) return;
    read = null; // a new photo: nothing from the last one carries over, even if this one fails
    const mine = ++attempt;
    const photo = await shrink(file);
    const url = URL.createObjectURL(photo);
    for (const img of root.querySelectorAll("[data-preview]")) img.src = url;
    step("reading");
    // Set after the step shows, so a screen reader announces it.
    setTimeout(() => { root.querySelector("[data-reading-status]").textContent = "Reading the label. Usually 2 to 5 seconds."; }, 50);
    const id = uuid();
    try {
      const r = await fetch(`/api/photos/${id}`, {
        method: "POST",
        headers: { "Content-Type": photo.type || "image/jpeg", "X-Requested-With": "fetch", "X-Expected-User": ME },
        body: photo,
      });
      const body = await r.json().catch(() => ({}));
      if (mine !== attempt) return;
      // 403 (another account signed in meanwhile, or the origin check): held in the outbox like an offline photo.
      if (r.status === 403) throw new Held(body.detail || "The server refused this for now.");
      if (!r.ok) return failed("Couldn’t use that photo", body.detail || "Try again, or type the coffee.", r.status < 500);
      read = body;
      tick(8);
      if (body.status === "read" && body.match) return showMatch(body.match);
      if (body.status === "read") return showPick();
      const why = {
        illegible: "The label was too dark or out of focus. Retake it with the label facing the light, or type the coffee.",
        limit: "That’s today’s limit of label reads. Type the coffee, or try the photo again tomorrow.",
        no_key: "Reading labels isn’t set up on this server. Type the coffee.",
      }[body.status] || "The label couldn’t be read just now. Retake it, or type the coffee.";
      const canRetake = !["limit", "no_key"].includes(body.status);
      failed(body.status === "limit" ? "No more reads today" : "Couldn’t read the bag", why, canRetake);
    } catch {
      if (mine !== attempt) return;
      // Offline (or held): the photo waits in the outbox and goes up with the bag; the server reads it when it arrives.
      await store("readwrite", (s) => s.put({ key: `photo:${id}`, url: `/api/photos/${id}`, body: null, blob: photo, at: Date.now(), user: ME }));
      read = { photo_id: id, raw: null, pending: true }; // not read yet: the server may match it later
      showSync();
      root.querySelector("[data-photo-waiting]").hidden = false;
      step("queued");
    }
  });

  const typedSave = form.querySelector('[type="submit"]');
  const typedLabel = armRoastCheck(form, typedSave);
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const coffee = form.coffee_freetext.value.trim();
    if (!coffee) return hint(form, "Type the roaster and coffee.");
    const said = roastCheck(form, typedSave, typedLabel);
    if (said) return hint(form, said.trim());
    hint(form, "");
    const body = { coffee_freetext: coffee };
    if (form.roast_date.value) body.roast_date = form.roast_date.value;
    save(body, form);
  });
}

function equipment(form) {
  const choices = form.querySelectorAll("[data-choice]");
  for (const choice of choices) {
    choice.addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (b) { pressOne(choice, b); tick(); }
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
    const next = form.dataset.next || "/setup"; // back where it was opened from: Setup, first run, the gear picker
    const did = extra.retired ? "retired" : form.dataset.id ? "saved" : "added";
    try {
      await send(`equipment:${body.equipment_id}`, "/api/equipment", body);
      go(next, `${body.name} ${did}`);
    } catch (err) {
      if (err instanceof Rejected) return hint(form, why(err));
      go(next, OFFLINE);
    }
  }
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    save({});
  });
  const retire = form.querySelector("[data-retire]");
  retire?.addEventListener("click", () => confirmTap(retire, () => save({ retired: true }))); // it leaves the pickers
}

// --- The sign-up rules --------------------------------------------------------------------------------
// The same rules and words as the server (app/rules.py), from the page's JSON block, so a verdict here is the
// server's verdict (tests/test_rules.py holds the two to that). Each check returns every failing rule.

const RULES = (() => {
  try {
    return JSON.parse(document.getElementById("rules")?.textContent || "null");
  } catch {
    return null;
  }
})();
const COMMON = new Set(RULES?.common || []);
const points = (s) => [...s].length; // code points, as Python's len()

function ruleError(rule, message = null) {
  return { field: RULES.rules[rule].field, rule, message: message || RULES.rules[rule].message };
}

function cleanUsername(name) {
  return name.normalize("NFC").trim().toLowerCase();
}

function usernameSuggestion(name) {
  if (!name.includes("@")) return null;
  const s = cleanUsername(name).split("@")[0].replace(new RegExp(`[^${RULES.username.chars}]`, "gu"), "").slice(0, RULES.username.max);
  return points(s) >= RULES.username.min ? s : null;
}

function checkUsername(name) {
  const u = cleanUsername(name);
  const out = [];
  if (u.includes("@")) {
    const s = usernameSuggestion(u);
    out.push(ruleError("u.email", s ? RULES.suggest.replace("{suggestion}", s) : null));
  }
  const n = points(u);
  if (n < RULES.username.min || n > RULES.username.max) out.push(ruleError("u.length"));
  const allowed = new RegExp(`^[${RULES.username.chars}]$`, "u");
  if ([...u].some((c) => !allowed.test(c))) out.push(ruleError("u.chars"));
  return out;
}

function checkPassword(password, username = "") {
  const pw = password.normalize("NFC");
  const name = cleanUsername(username);
  const out = [];
  const n = points(pw);
  if (n < RULES.password.min) out.push(ruleError("p.length"));
  else if (n > RULES.password.max) out.push(ruleError("p.length", RULES.too_long));
  const folded = pw.toLowerCase();
  if (COMMON.has(folded)) out.push(ruleError("p.common"));
  if (name && folded.includes(name)) out.push(ruleError("p.username"));
  if (new Set([...pw]).size < RULES.password.distinct) out.push(ruleError("p.variety"));
  return out;
}

// What rules.TRIM holds: JavaScript's trim() set, so "no spaces" means the same on both sides.
const SPACES = new RegExp("[\\t\\n\\v\\f\\r \\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff]", "u");

function cleanEmail(email) {
  const e = email.normalize("NFC").trim();
  const at = e.lastIndexOf("@");
  return at < 0 ? e : `${e.slice(0, at)}@${e.slice(at + 1).toLowerCase()}`;
}

// rules.check_email: one @, something before it, a dot inside the part after it, no spaces, at most 254.
function checkEmail(email, required = false) {
  const e = cleanEmail(email);
  if (!e) return required ? [ruleError("e.required")] : [];
  const at = e.indexOf("@");
  const local = at < 0 ? e : e.slice(0, at);
  const domain = at < 0 ? "" : e.slice(at + 1);
  const ok = (e.match(/@/g) || []).length === 1 && local !== "" && domain.replace(/^\.+|\.+$/g, "").includes(".")
    && points(e) <= RULES.email_max && !SPACES.test(e);
  return ok ? [] : [ruleError("e.syntax")];
}

// --- Forms: errors under their fields, busy and slow states ---------------------------------------------

const UNREACHABLE = "Couldn’t reach the app. Nothing you typed is lost. Try again in a moment.";
const OUR_SIDE = "Something went wrong on our side. Try again.";
const SLOW = "Still working. The app may be waking up, which can take a minute or two. Keep this page open.";
const SLOWER = "Still working. Saving can take a little longer when the app has just woken up.";
const SLOW_SAVE = "Still saving. This takes a few seconds when the app has just woken up.";
const RETRY_WAITS = [2000, 5000, 10000]; // a lost reply is sent again, the same body, this far apart
const sleep = (ms) => new Promise((resolve) => { setTimeout(resolve, ms); });

// The form-level line (role="alert"): emptied, then filled a moment later, so the same words twice are read again.
function say(root, text) {
  const el = root.querySelector("[data-hint]");
  if (!el) return;
  el.textContent = "";
  if (text) setTimeout(() => hint(root, text), 100);
}

// One field's error line, under it, and aria-invalid on the field. A hint that doubles as the error line (the
// invite code's) goes back to its own words when cleared. "Already used" links "sign in" to the sign-in screen.
function setError(form, field, message = "", rule = null, mark = true) {
  const slot = form.querySelector(`[data-error-for="${field}"]`);
  const input = form.querySelector(`[name="${field}"]`);
  if (input) {
    if (message && mark) input.setAttribute("aria-invalid", "true");
    else input.removeAttribute("aria-invalid");
  }
  if (!slot) return Boolean(input && message);
  if (slot.dataset.idle === undefined) slot.dataset.idle = slot.textContent;
  const text = message || slot.dataset.idle;
  slot.classList.toggle("is-error", Boolean(message) && !slot.classList.contains("ferr")); // a hint that now says what's wrong
  const [before, after] = text.split("sign in");
  if (rule === "c.used" && after !== undefined) {
    const a = document.createElement("a");
    a.href = "/signin";
    a.textContent = "sign in";
    slot.replaceChildren(before, a, after);
  } else {
    slot.textContent = text;
  }
  return true;
}

function clearErrors(form) {
  for (const slot of form.querySelectorAll("[data-error-for]")) setError(form, slot.dataset.errorFor);
  for (const el of form.querySelectorAll("[aria-invalid]")) el.removeAttribute("aria-invalid");
}

// Field errors in place; anything that belongs to no field in the form's line. Focus goes to the first field that
// failed, in screen order, so the keyboard doesn't hide the message.
function showErrors(form, errors, text = "") {
  let loose = text;
  for (const e of errors) {
    const slot = form.querySelector(`[data-error-for="${e.field}"]`);
    if (slot && slot.dataset.shown !== "1") {
      setError(form, e.field, e.message, e.rule);
      slot.dataset.shown = "1";
    } else if (!slot && !setError(form, e.field, e.message)) {
      loose = loose || e.message;
    }
  }
  for (const slot of form.querySelectorAll("[data-shown]")) delete slot.dataset.shown;
  say(form, loose);
  form.querySelector('[aria-invalid="true"]')?.focus();
}

// Leaving a field shows its errors, which moves what's below it. When the field was left by a tap (on the terms box,
// or the button), that waits until the tap is over, so the layout doesn't shift under the finger and the tap lands.
let pointerDown = false;
let afterTap = [];
addEventListener("pointerdown", () => { pointerDown = true; }, true);
addEventListener("pointerup", () => {
  pointerDown = false;
  const run = afterTap;
  afterTap = [];
  setTimeout(() => { for (const fn of run) fn(); }, 0); // after the click this pointerup ends
}, true);
const whenTapEnds = (fn) => (pointerDown ? afterTap.push(fn) : fn());

// The ✓/✗ list under a field and its error line. While typing, rules only turn met (and the others wait until the
// length is met); a failed rule turns red when the field is left or the form sent (validate on blur). Nothing is
// announced per keystroke: the list and the error are read with the field (aria-describedby).
function liveRules(form) {
  const user = form.querySelector('[data-check="username"]');
  const pass = form.querySelector('[data-check="password"]');
  const mail = form.querySelector('[data-check="email"]');
  if (!RULES || (!user && !pass && !mail)) return null;
  const strict = new Set();
  const nameHint = form.querySelector("[data-username-hint]");
  const suggest = form.querySelector("[data-suggest]");
  const failed = (input) => (input === user ? checkUsername(input.value)
    : input === mail ? checkEmail(input.value, input.dataset.required !== undefined && strict.has(input))
      : checkPassword(input.value, user ? user.value : form.dataset.username || ""));
  // The email has no rule list: its one error shows on blur or submit, and goes as soon as it's fixed.
  const paintMail = () => {
    const fails = failed(mail);
    if (strict.has(mail)) setError(form, "email", fails[0]?.message || "");
    else if (!fails.length) setError(form, "email");
    return fails;
  };
  const paint = (input) => {
    if (input === mail) return paintMail();
    const fails = failed(input);
    const ids = new Set(fails.map((e) => e.rule));
    const lengthRule = input === user ? "u.length" : "p.length";
    const lengthMet = input.value !== "" && !ids.has(lengthRule);
    const loud = strict.has(input);
    for (const li of document.querySelectorAll(`#${input.id}-rules li`)) {
      const rule = li.dataset.rule;
      // While the "@" line shows, the characters rule it breaks shows as failed too.
      const now = loud || (rule === "u.chars" && ids.has("u.email"));
      const state = ids.has(rule) ? (now ? "unmet" : "") : input.value && (rule === lengthRule || lengthMet) ? "met" : "";
      li.dataset.state = state;
      li.querySelector("[data-said]").textContent = state === "met" ? ", met" : state === "unmet" ? ", not met" : "";
    }
    // "@" is answered at once, with a one-tap fix; the rest waits for blur.
    const email = fails.find((e) => e.rule === "u.email");
    if (loud) setError(form, input.name, fails[0]?.message || "");
    else setError(form, input.name, email ? email.message : "", null, false);
    if (loud && !fails.length) input.removeAttribute("aria-invalid");
    if (input === user) {
      const s = email && usernameSuggestion(input.value);
      suggest.hidden = !s;
      if (s) suggest.querySelector("button").textContent = `Use ${s}`;
      const lower = cleanUsername(input.value);
      nameHint.textContent = lower !== input.value.trim() && !fails.length ? `You’ll sign in as ${lower}.` : "You’ll sign in with it. Not your email.";
    }
    return fails;
  };
  for (const input of [user, mail, pass].filter(Boolean)) {
    input.addEventListener("input", () => { paint(input); if (input === user && pass) paint(pass); });
    input.addEventListener("blur", () => whenTapEnds(() => { if (input.value) strict.add(input); paint(input); }));
  }
  suggest?.querySelector("button").addEventListener("click", () => {
    user.value = usernameSuggestion(user.value) || user.value;
    strict.add(user);
    paint(user);
    if (pass) paint(pass);
    user.focus();
  });
  return {
    check() {
      const out = [];
      for (const input of [user, mail, pass].filter(Boolean)) {
        strict.add(input);
        out.push(...paint(input).map((e) => ({ ...e, field: input.name })));
      }
      return out;
    },
  };
}

// The bag page's filling cup in a busy button, before its label; gone again when it's done.
function busyCup(b, on) {
  b.querySelector(":scope > .cup")?.remove();
  const cup = on && document.getElementById("busy-cup")?.content.firstElementChild;
  if (cup) b.prepend(cup.cloneNode(true));
}

// Busy: the button says what's happening and ignores more taps (aria-disabled, so focus stays on it), the fields
// can't change underneath the request, and a line under the button explains a wait past 4 s.
function busyState(form, submit) {
  const idle = submit.textContent;
  const status = form.querySelector("[data-status]");
  let timers = [];
  let locked = [];
  return {
    get on() { return submit.getAttribute("aria-disabled") === "true"; },
    start(text) {
      submit.setAttribute("aria-disabled", "true");
      submit.textContent = text;
      busyCup(submit, true);
      locked = [...form.querySelectorAll("input:not([type=checkbox]):not([readonly])")];
      for (const el of locked) el.readOnly = true;
      timers = [setTimeout(() => { if (status) status.textContent = SLOW; }, 4000),
        setTimeout(() => { if (status) status.textContent = SLOWER; }, 45000)];
    },
    stop(text = idle) {
      for (const t of timers) clearTimeout(t);
      if (status) status.textContent = "";
      submit.removeAttribute("aria-disabled");
      submit.textContent = text;
      busyCup(submit, false);
      for (const el of locked) el.readOnly = false;
      locked = [];
    },
  };
}

// The form's body: each named input's value, and a checkbox's tick.
function formBody(form) {
  const body = {};
  for (const el of form.querySelectorAll("input[name]")) body[el.name] = el.type === "checkbox" ? el.checked : el.value;
  return body;
}

// Join is idempotent on the server, so a reply that never arrived (a timeout, a dropped connection, a 502/503/504
// while the app wakes) is sent again, the identical body, up to three times.
async function postRetrying(url, body) {
  for (let i = 0; ; i++) {
    try {
      return await post(url, body);
    } catch (err) {
      if (!(err instanceof Unreachable) || i >= RETRY_WAITS.length) throw err;
      await sleep(RETRY_WAITS[i]);
    }
  }
}

// Sign in, owner setup, join, reset, accepting new terms and changing the password: one pattern. `done` runs on
// success; by default the page moves on to Today.
function accountForm(form, done = null) {
  const submit = form.querySelector('[type="submit"]');
  const busy = busyState(form, submit);
  const live = liveRules(form);
  // Sign-up's optional email: asked about once, on submit, when it's empty (spec §5). Add email goes to the field;
  // Skip sends the form as it is.
  const nudge = form.querySelector("[data-email-nudge]");
  let nudged = false;
  if (nudge) {
    nudge.querySelector("[data-nudge-add]").addEventListener("click", () => {
      nudge.hidden = true;
      form.querySelector('[name="email"]').focus();
    });
    nudge.querySelector("[data-nudge-skip]").addEventListener("click", () => {
      nudge.hidden = true;
      form.requestSubmit(submit);
    });
  }
  // A tick can't change while the request is out.
  form.addEventListener("click", (e) => { if (busy.on && e.target.matches?.("input[type=checkbox]")) e.preventDefault(); });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (busy.on) return; // repeat taps do nothing
    clearErrors(form);
    const errors = live ? live.check() : [];
    let empty = false;
    for (const el of form.querySelectorAll("input[required]")) {
      if (el.type === "checkbox") {
        if (!el.checked) errors.push(ruleError("terms", form.id === "agree-form" ? RULES.terms_again : null));
      } else if (!el.dataset.check && !el.value.trim()) {
        errors.push({ field: el.name, message: el.dataset.empty || "" }); // its own words, or the form's line
        empty = empty || !el.dataset.empty;
      }
    }
    if (errors.length) {
      for (const err of errors.filter((x) => !x.message)) form.querySelector(`[name="${err.field}"]`)?.setAttribute("aria-invalid", "true");
      // Errors are shown in the order of the fields on screen.
      const order = [...form.querySelectorAll("input[name]")].map((el) => el.name);
      errors.sort((a, b) => order.indexOf(a.field) - order.indexOf(b.field));
      return showErrors(form, errors.filter((err) => err.message), empty ? "Fill in every field." : "");
    }
    if (nudge && !nudged && !form.querySelector('[name="email"]').value.trim()) {
      nudged = true;
      nudge.hidden = false;
      nudge.querySelector("#nudge-text").focus(); // read out, and the two choices are next
      return;
    }
    nudge && (nudge.hidden = true);
    const body = formBody(form);
    busy.start(form.dataset.busy || "Saving…");
    try {
      const url = form.dataset.api || "/api/signin";
      const reply = await (form.dataset.retry !== undefined ? postRetrying(url, body) : post(url, body));
      busy.stop(form.dataset.done || submit.textContent);
      submit.setAttribute("aria-disabled", "true"); // nothing more to send from this page
      busyCup(submit, true); // the next page is loading
      if (done) return done(reply, body);
      try {
        sessionStorage.removeItem("join-code");
        sessionStorage.removeItem("reset-code");
        if (form.id === "join" || form.dataset.ownerSetup === "true") {
          const welcome = `Welcome, ${cleanUsername(body.username)}.`;
          sessionStorage.setItem("notice", reply.email_sent ? `${welcome} Check your inbox for a link to confirm your email.` : welcome);
        }
        if (reply.signin) { // a reset link from email ends on sign-in, with the username filled in (spec §4)
          sessionStorage.setItem("notice", "Password changed. Sign in with the new one.");
          sessionStorage.setItem("signin-username", reply.signin);
        }
      } catch { /* storage is optional */ }
      if (reply.signin) return location.replace("/signin");
      // Pages kept for offline use belong to whoever was signed in before: they go before this person's first page.
      // Some WebViews have no `caches` at all; the account is made by now, so that mustn't look like a failure.
      try { await globalThis.caches?.delete("pages"); } catch { /* nothing kept */ }
      location.replace("/");
    } catch (err) {
      if (err instanceof Unreachable) {
        busy.stop("Try again");
        return say(form, UNREACHABLE);
      }
      busy.stop();
      if (err.status >= 500) return say(form, OUR_SIDE);
      if (err.errors?.length) return showErrors(form, err.errors);
      say(form, err.message);
    }
  });
}

// --- Joining and inviting -----------------------------------------------------------------------------

// A link carries its code after #, which never reaches the server. It's kept for this tab (sessionStorage) before
// it leaves the address bar, so a reload still has it; it's cleared once the account is made.
function codeFromLink(form, key, check = null) {
  const input = form.querySelector("#code");
  let fromLink = "";
  try {
    fromLink = decodeURIComponent(location.hash.slice(1));
  } catch {
    // A link that was cut or mangled in the message: say so, and keep it out of history.
    history.replaceState(null, "", location.pathname);
    setError(form, "code", "That link didn’t come through whole. Type the code from the message.");
    return;
  }
  if (fromLink) {
    try { sessionStorage.setItem(key, fromLink); } catch { /* storage is optional */ }
    input.value = fromLink;
    history.replaceState(null, "", location.pathname); // keep the code out of history and screenshots
  } else if (!input.value) {
    try { input.value = sessionStorage.getItem(key) || ""; } catch { /* storage is optional */ }
  }
  if (input.value && check) check();
}

// The join screen says whether the code is good, or already used (with a way to sign in).
function joinCode(form) {
  const input = form.querySelector("#code");
  const status = form.querySelector("[data-code-status]");
  const check = async () => {
    if (!input.value.trim()) return;
    try {
      const r = await post("/api/invites/check", { code: input.value });
      // What the line goes back to when other errors are cleared on submit.
      status.dataset.idle = r.kind === "join" ? `Valid until ${r.expires}.` : "That’s a reset code. Use it on the reset screen.";
      setError(form, "code");
    } catch (err) {
      const e = err.errors?.[0];
      setError(form, "code", e ? e.message : err instanceof Rejected ? err.message : UNREACHABLE, e?.rule);
    }
  };
  codeFromLink(form, "join-code", check);
  input.addEventListener("change", check);
}

// In Instagram's or Facebook's browser: copy this page's link (the code from the field, if any) to open elsewhere.
// If the clipboard says no, the link is shown to press and hold.
function inAppBanner(box) {
  const button = box.querySelector("[data-copy-link]");
  button.addEventListener("click", async () => {
    const code = document.getElementById("code")?.value.trim();
    const link = `${location.origin}${location.pathname}${code ? `#${code}` : ""}`;
    if (await copyText(link)) {
      button.textContent = "Copied";
      setTimeout(() => { button.textContent = "Copy link"; }, 2000);
      return;
    }
    const fallback = box.querySelector("[data-copy-fallback]");
    const field = fallback.querySelector("input");
    fallback.hidden = false;
    field.value = link;
    field.focus();
    field.select();
    field.scrollLeft = 0; // the start of the link (the host) in view
  });
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false; // no clipboard API, or not allowed: the code stays on screen to copy by hand
  }
}

// An invite's "For" note: what it'll be stored as (trimmed, spaces collapsed), counted as the server counts.
// The same cleaning as rules.clean_label: control characters (other than whitespace) dropped, NFC, then split on
// what Python's str.split() counts as whitespace.
const PY_SPACE = "\\t\\n\\v\\f\\r\\x1c-\\x1f \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000";
const cleanNote = (text) => text.replace(new RegExp(`(?![${PY_SPACE}])[\\x00-\\x1f\\x7f-\\x9f]`, "g"), "").normalize("NFC")
  .split(new RegExp(`[${PY_SPACE}]+`)).filter(Boolean).join(" ");
const noteText = (input) => cleanNote(input.value);
function noteCounter(input) {
  const count = input.closest("[data-new-note], [data-note-edit]").querySelector("[data-count]");
  const update = () => { count.textContent = `${points(noteText(input))} of ${RULES.label_max}`; };
  input.addEventListener("input", update);
  update();
}

function invites() {
  const box = document.querySelector("[data-codebox]");
  const primary = document.querySelector("[data-new-invite]");
  const another = document.querySelector("[data-another]");
  const intro = document.querySelector("[data-code-intro]");
  const status = document.querySelector("main [data-status]");
  const note = document.querySelector("[data-new-note]");
  const main = document.querySelector("main");
  const tooLong = () => RULES.rules["i.label"].message;
  const show = (r, name) => {
    const join = r.kind === "join";
    const link = `${location.origin}/${join ? "join" : "reset"}#${r.code}`;
    const groups = r.code.split("-");
    const [a, b] = box.querySelectorAll("[data-code] span");
    a.textContent = `${groups[0]}-${groups[1]}`; // two groups per line, never broken inside one
    b.textContent = `${groups[2]}-${groups[3]}`;
    const label = box.querySelector("[data-code-label]");
    label.textContent = join ? (r.for_label ? `Invite for ${r.for_label}` : "Invite code") : `Reset link for ${name}`;
    box.querySelector("[data-code-note]").textContent = join
      ? `${location.host}/join · until ${r.expires}. Shown only once: send it before you leave this screen.`
      : `${location.host}/reset · Send it to ${name}. It works once, until ${r.expires}, and signs them out everywhere.`;
    intro.textContent = join ? `Share the link. It works once and expires ${r.expires}.` : `Send ${name} the link below.`;
    box.hidden = false;
    note.hidden = true;
    note.querySelector("input").value = "";
    note.querySelector("input").dispatchEvent(new Event("input"));
    // The one filled button now sends this link; making another one is a text button with a second tap.
    const share = Boolean(navigator.share);
    const idle = share ? "Share link" : "Copy link";
    primary.textContent = idle;
    primary.onclick = async () => {
      if (share) return navigator.share({ title: "registry.coffee", url: link }).catch(() => {});
      if (await copyText(link)) {
        say(main, "");
        primary.textContent = "Copied";
        setTimeout(() => { primary.textContent = idle; }, 2000);
      } else {
        say(main, "Couldn’t copy. Press and hold the code to copy it.");
      }
    };
    another.hidden = !join;
    box.scrollIntoView({ block: "center" });
    label.focus({ preventScroll: true });
    status.textContent = join ? "Invite link ready. It’s shown only once." : "Reset link ready. It’s shown only once.";
  };
  // Every action: its own busy label, no second request while one is out, and a line if it takes a while.
  const act = async (b, busyText, url, body, after) => {
    if (b.getAttribute("aria-disabled") === "true") return;
    const idle = b.textContent;
    b.setAttribute("aria-disabled", "true");
    b.textContent = busyText;
    busyCup(b, true);
    say(main, "");
    status.textContent = "";
    const slow = setTimeout(() => { status.textContent = SLOW_SAVE; }, 4000);
    try {
      const r = await post(url, body);
      clearTimeout(slow);
      status.textContent = "";
      if (b.textContent === busyText) b.textContent = idle;
      b.removeAttribute("aria-disabled");
      busyCup(b, false);
      after(r);
    } catch (err) {
      clearTimeout(slow);
      status.textContent = "";
      b.textContent = idle;
      b.removeAttribute("aria-disabled");
      busyCup(b, false);
      const e = err.errors?.[0];
      if (e?.field === "for_label") {
        const scope = b.closest("[data-note-edit]") || note;
        setError(scope, "for_label", e.message);
        scope.querySelector("input").setAttribute("aria-invalid", "true");
        scope.querySelector("input").focus();
      } else {
        say(main, e ? e.message : err instanceof Rejected ? err.message : UNREACHABLE);
      }
    }
  };
  const noteField = note.querySelector("input");
  noteField.name = "for_label";
  noteCounter(noteField);
  const makeOne = (b) => {
    setError(note, "for_label");
    const label = noteText(noteField);
    if (points(label) > RULES.label_max) {
      setError(note, "for_label", tooLong());
      noteField.setAttribute("aria-invalid", "true");
      return noteField.focus();
    }
    return act(b, "Making a link…", "/api/invites", label ? { for_label: label } : {}, (r) => show(r));
  };
  primary.onclick = () => makeOne(primary);
  // "Make another link" brings the For box back first; the second tap makes the link, with its note.
  another.onclick = () => {
    note.hidden = false;
    confirmTap(another, () => makeOne(another));
  };
  for (const b of document.querySelectorAll("[data-reset]")) {
    b.onclick = () => confirmTap(b, () => act(b, "Making a reset link…", "/api/invites", { kind: "reset", for_user_id: b.dataset.reset }, (r) => show(r, b.dataset.name)));
  }
  for (const b of document.querySelectorAll("[data-renew]")) {
    b.onclick = () => act(b, "Making a new link…", `/api/invites/${b.dataset.renew}/renew`, {}, (r) => {
      b.previousElementSibling.hidden = true;
      b.nextElementSibling.hidden = true;
      b.hidden = true;
      show(r);
    });
  }
  for (const b of document.querySelectorAll("[data-revoke]")) {
    b.onclick = () => act(b, "Revoking…", `/api/invites/${b.dataset.revoke}/revoke`, {}, () => go(location.pathname, "Invite revoked"));
  }
  for (const b of document.querySelectorAll("[data-remove-email]")) {
    b.onclick = () => confirmTap(b, () => act(b, "Removing…", `/api/people/${b.dataset.removeEmail}/email/remove`, {},
      () => go(location.pathname, `${b.dataset.name}’s email removed`)));
  }
  for (const b of document.querySelectorAll("[data-person]")) {
    const off = b.dataset.disabled === "true";
    b.onclick = () => confirmTap(b, () => act(b, off ? "Turning off…" : "Turning on…", `/api/people/${b.dataset.person}`, { disabled: off },
      () => go(location.pathname, off ? `${b.dataset.name}’s account is turned off` : `${b.dataset.name}’s account is back on`)));
  }
  // Edit note, in place: Save (empty clears it) or Cancel.
  for (const row of document.querySelectorAll("[data-invite]")) {
    const edit = row.querySelector("[data-note-edit]");
    const open = row.querySelector("[data-edit-note]");
    const field = edit.querySelector("input");
    const shown = row.querySelector("[data-label]");
    field.name = "for_label";
    noteCounter(field);
    const close = () => {
      edit.hidden = true;
      open.hidden = false;
      open.setAttribute("aria-expanded", "false");
      setError(edit, "for_label");
      open.focus();
    };
    open.onclick = () => {
      edit.hidden = false;
      open.hidden = true; // Save and Cancel close it
      open.setAttribute("aria-expanded", "true");
      field.focus();
    };
    edit.querySelector("[data-cancel-note]").onclick = () => {
      field.value = shown.dataset.saved ?? (shown.textContent === "Invite" ? "" : shown.textContent);
      field.dispatchEvent(new Event("input"));
      close();
    };
    const save = edit.querySelector("[data-save-note]");
    save.onclick = () => {
      setError(edit, "for_label");
      const label = noteText(field);
      if (points(label) > RULES.label_max) {
        setError(edit, "for_label", tooLong());
        field.setAttribute("aria-invalid", "true");
        return field.focus();
      }
      return act(save, "Saving…", `/api/invites/${row.dataset.invite}/label`, { for_label: label }, (r) => {
        shown.textContent = r.for_label || "Invite";
        shown.dataset.saved = r.for_label || "";
        field.value = r.for_label || "";
        close();
        status.textContent = r.for_label ? "Note saved." : "Note cleared.";
      });
    };
  }
}

// A shot's Retract: a second tap, then a retraction record (never a delete), queued like any write.
// Retract: no confirmation; the shot goes at once and an undo strip stays for 5 s (DESIGN, "Undo").
function retractShot(b) {
  const id = b.dataset.retract;
  const actions = document.querySelector("[data-actions]");
  const strip = document.querySelector("[data-restore]");
  let timer = null;
  let gone = "Shot retracted";
  const leave = () => { timer = setTimeout(() => go(b.dataset.back, gone), 5000); };
  strip.addEventListener("focusin", () => clearTimeout(timer)); // focus on Undo holds it open
  strip.addEventListener("focusout", leave);
  b.addEventListener("click", async () => {
    b.disabled = true;
    try {
      await send(`retract:${id}`, `/api/shots/${id}/retract`, {});
    } catch (err) {
      if (err instanceof Rejected) {
        hint(document.querySelector("main"), err.message);
        b.disabled = false;
        return;
      }
      // No connection: the retraction waits in the outbox and goes through later, so it's done as far as this
      // screen goes (#18-3), and Undo can still take it back.
      strip.firstChild.textContent = gone = "Retracted on this phone. It syncs when you’re back online.";
    }
    tick(8);
    actions.hidden = true;
    strip.hidden = false;
    for (const el of document.querySelector("main").children) if (!el.matches(".top, .undo, [data-actions]")) el.classList.add("gone");
    leave();
  });
  strip.querySelector("button").addEventListener("click", async () => {
    clearTimeout(timer);
    try {
      // A retraction still on the phone is simply dropped; one that went up is undone on the server.
      if (await store("readonly", (s) => s.get(`retract:${id}`))) await store("readwrite", (s) => s.delete(`retract:${id}`));
      else await send(`restore:${id}`, `/api/shots/${id}/restore`, {});
      go(location.pathname + location.search, "Shot restored");
    } catch (err) {
      strip.firstChild.textContent = why(err);
    }
  });
}

// Theme and hand live in cookies so the server renders them; no flash of the other layout.
function preference(group, name, plain) {
  group.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    pressOne(group, b);
    tick();
    const v = b.dataset.v;
    document.cookie = `${name}=${v}; path=/; max-age=31536000; samesite=lax${location.protocol === "https:" ? "; secure" : ""}`;
    if (v === plain) delete document.documentElement.dataset[name];
    else document.documentElement.dataset[name] = v;
  });
}

// --- Signing in and the account -----------------------------------------------------------------------

class Unreachable extends Error {} // no reply: offline, timed out, or a gateway error while the app wakes up

// A JSON POST with a deadline: past it (130 s, just over Cloud Run's 120 s request limit) the request is given up.
// A refusal carries the server's words: a plain string, or a list of {field, rule, message} for errors that belong
// to a field.
async function post(url, body, { timeout = 130000 } = {}) {
  const ctrl = typeof AbortController === "function" ? new AbortController() : null;
  const timer = ctrl && setTimeout(() => ctrl.abort(), timeout);
  let r;
  try {
    r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Requested-With": "fetch" },
      body: JSON.stringify(body),
      signal: ctrl?.signal,
    });
  } catch {
    throw new Unreachable(UNREACHABLE);
  } finally {
    clearTimeout(timer);
  }
  if (r.ok) return r.json();
  if ([502, 503, 504].includes(r.status)) throw new Unreachable(UNREACHABLE);
  const detail = (await r.json().catch(() => ({}))).detail;
  const list = Array.isArray(detail) ? detail : [];
  const words = typeof detail === "string" ? detail : list.map((d) => (typeof d === "string" ? d : d?.message)).filter(Boolean).join(" ");
  const err = new Rejected(words || (r.status >= 500 ? OUR_SIDE : "Something went wrong. Try again."));
  err.status = r.status;
  err.errors = list.filter((d) => d && typeof d === "object" && d.field);
  return Promise.reject(err);
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

function account() {
  const form = document.getElementById("password-form");
  if (form) {
    accountForm(form, (reply, body) => {
      form.reset();
      form.querySelector('[type="submit"]').removeAttribute("aria-disabled");
      form.querySelector('[type="submit"]').textContent = "Change password";
      for (const li of form.querySelectorAll(".rules li")) li.dataset.state = "";
      form.closest("details").open = false;
      notify("Password changed. You’re signed out on your other devices.");
      scrollTo(0, 0);
      return body;
    });
  }
  for (const b of document.querySelectorAll("[data-signout]")) {
    // Signing out everywhere asks for a second tap (data-confirm); plain sign-out doesn't.
    b.addEventListener("click", () => confirmTap(b, async () => {
      try {
        await post(b.dataset.signout, {});
        // Kept pages hold this person's data: they go with the session, once the server has signed it out.
        try { await globalThis.caches?.delete("pages"); } catch { /* nothing kept */ }
        navigator.serviceWorker?.controller?.postMessage("signed-out");
        location.replace("/signin");
      } catch {
        b.textContent = "Couldn’t sign out. Check the connection and try again.";
      }
    }));
  }
}

// --- Email: recovery, verification and the Setup row (docs/app/signup-ux-spec.md §4, §5) -------------

// One reply for an account with a verified email and for no account at all (owner decision, 2026-11-08: only an
// account with no verified email is told so, on the field).
const SENT = "If that account has a verified email, we’ve sent a link to it. It works for 30 minutes. Check your spam folder if it isn’t there in a few minutes.";

function forgotForm(form) {
  const submit = form.querySelector('[type="submit"]');
  const idle = submit.textContent;
  accountForm(form, () => {
    submit.removeAttribute("aria-disabled");
    busyCup(submit, false);
    submit.textContent = idle;
    notify(SENT);
    scrollTo(0, 0);
  });
}

// A verify link carries its token after #, which never reaches the server's logs; it leaves the address bar at once.
async function verifyPage(box) {
  const title = box.querySelector("[data-verify-title]");
  const status = box.querySelector("[data-verify-status]");
  const next = document.querySelector("[data-verify-next]");
  let token = "";
  try { token = decodeURIComponent(location.hash.slice(1)); } catch { /* a mangled link: treated as expired */ }
  history.replaceState(null, "", location.pathname);
  const expired = () => {
    title.textContent = "Link expired";
    const a = document.createElement("a");
    a.href = "/setup#email";
    a.textContent = "Send a new one from Setup";
    status.replaceChildren("That link has expired or was already used. ", a, ".");
  };
  if (!token) return expired();
  try {
    await post("/api/verify", { token });
    title.textContent = "Email verified";
    status.textContent = "You can now reset your password by email.";
    next.hidden = false;
  } catch (err) {
    if (err instanceof Unreachable) {
      status.textContent = UNREACHABLE;
      // keep the token for a reload
      history.replaceState(null, "", `${location.pathname}#${encodeURIComponent(token)}`);
      return;
    }
    expired();
  }
  title.focus();
}

function emailSettings() {
  const form = document.getElementById("email-form");
  if (!form) return;
  const edit = form.closest("details");
  if (location.hash === "#email" && !document.querySelector("[data-email-remove]")) {
    edit.open = true; // "Add email" from Today
    form.querySelector('[name="email"]').focus();
  }
  accountForm(form, (reply) => go("/setup", reply.verified ? "Email saved." : `Check your inbox for a link to confirm ${reply.email}.`));
  const resend = document.querySelector("[data-email-resend]");
  resend?.addEventListener("click", async () => {
    if (resend.getAttribute("aria-disabled") === "true") return;
    const idle = [...resend.childNodes].map((n) => n.cloneNode(true));
    resend.setAttribute("aria-disabled", "true");
    resend.textContent = "Sending…";
    busyCup(resend, true);
    try {
      await post("/api/email/resend", {});
      resend.replaceChildren(...idle);
      notify("Sent. The new link works for 24 hours; the old one no longer does.");
    } catch (err) {
      resend.replaceChildren(...idle);
      notify(err instanceof Rejected ? err.message : UNREACHABLE);
    }
    resend.removeAttribute("aria-disabled");
    scrollTo(0, 0);
  });
  const remove = document.querySelector("[data-email-remove]");
  remove?.addEventListener("click", () => confirmTap(remove, async () => {
    remove.setAttribute("aria-disabled", "true");
    remove.textContent = "Removing…";
    busyCup(remove, true);
    try {
      await post("/api/email/remove", {});
      go("/setup", remove.dataset.done);
    } catch (err) {
      remove.removeAttribute("aria-disabled");
      busyCup(remove, false);
      remove.textContent = "Remove email";
      notify(err instanceof Rejected ? err.message : UNREACHABLE);
      scrollTo(0, 0);
    }
  }));
  // The marketing box saves as it's ticked; the line under it says so (or puts the tick back).
  const box = document.querySelector("[data-marketing]");
  const said = document.querySelector("[data-marketing-status]");
  const quiet = said?.textContent || "";
  box?.addEventListener("change", async () => {
    const on = box.checked;
    said.textContent = "Saving…";
    try {
      await post("/api/marketing", { marketing: on });
      said.textContent = on ? `Saved: you’ll get them${quiet ? " once your email is verified" : ""}.` : "Saved: you won’t get them.";
    } catch (err) {
      box.checked = !on;
      said.textContent = err instanceof Rejected ? err.message : UNREACHABLE;
    }
  });
}

// Today's "Add an email" notice: Not now hides it for 90 days (the server reads the cookie).
function emailPrompt(box) {
  box.querySelector("[data-not-now]").addEventListener("click", () => {
    document.cookie = `nudge-email=1; path=/; max-age=${90 * 86400}; samesite=lax${location.protocol === "https:" ? "; secure" : ""}`;
    box.remove();
    const heading = document.querySelector("main h1"); // focus stays on the page, not lost with the notice
    if (heading) { heading.tabIndex = -1; heading.focus(); }
  });
}

document.addEventListener("DOMContentLoaded", () => {
  if (stale(false)) return location.reload();
  try {
    const notice = sessionStorage.getItem("notice"); // left by the save that led here
    sessionStorage.removeItem("notice");
    if (notice) notify(notice);
  } catch {
    // storage blocked: no notice
  }
  const shot = document.getElementById("shot");
  if (shot) logShot(shot);
  const strip = document.querySelector("[data-undo]");
  if (strip) undo(strip);
  const bag = document.getElementById("newbag");
  if (bag) newBag(bag);
  const bagForm = document.getElementById("bagedit");
  if (bagForm) bagEdit(bagForm);
  for (const f of document.querySelectorAll("[data-equipment]")) equipment(f);
  const theme = document.querySelector("[data-theme-choice]");
  if (theme) preference(theme, "theme", "auto");
  const hand = document.querySelector("[data-hand-choice]");
  if (hand) preference(hand, "hand", "right");
  // On the sign-in screen nobody is signed in, however the last session ended (signed out, expired, ended on
  // another phone): the pages kept for offline use held that person's data, so they go too.
  if (document.getElementById("signin")) {
    globalThis.caches?.delete("pages").catch(() => { /* nothing kept */ });
    navigator.serviceWorker?.controller?.postMessage("signed-out");
  }
  const signin = document.getElementById("signin");
  if (signin && signin.dataset.ownerSetup !== "true") {
    try { // after a reset by email: the username they reset, filled in
      const name = sessionStorage.getItem("signin-username");
      sessionStorage.removeItem("signin-username");
      if (name) {
        signin.querySelector("#username").value = name;
        signin.querySelector("#password").focus();
      }
    } catch { /* storage is optional */ }
  }
  for (const id of ["signin", "join", "reset", "agree-form"]) {
    const f = document.getElementById(id);
    if (f) accountForm(f);
  }
  const forgot = document.getElementById("forgot");
  if (forgot) forgotForm(forgot);
  const verify = document.querySelector("[data-verify]");
  if (verify) verifyPage(verify);
  emailSettings();
  const prompt = document.querySelector("[data-email-prompt]");
  if (prompt) emailPrompt(prompt);
  const join = document.getElementById("join");
  if (join) joinCode(join);
  const reset = document.getElementById("reset");
  if (reset) codeFromLink(reset, "reset-code");
  const inApp = document.querySelector("[data-in-app]");
  if (inApp) inAppBanner(inApp);
  if (document.querySelector("[data-new-invite]")) invites();
  for (const b of document.querySelectorAll("[data-retract]")) retractShot(b);
  for (const b of document.querySelectorAll("[data-show]")) showPassword(b);
  for (const b of document.querySelectorAll("[data-reload]")) b.addEventListener("click", () => location.reload());
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
