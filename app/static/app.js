// registry.coffee: the outbox, the shot form, undo and the setup forms. No build step.

// --- the outbox --------------------------------------------------------------------------------------
// Every write waits in IndexedDB until the server acknowledges it, keyed by the id the phone generated, so a
// dropped connection never loses a shot and a retry never makes a second one.

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
  const why = Array.isArray(detail) ? detail.join(" ") : detail || r.statusText;
  if (RETRY.has(r.status) || r.status >= 500) {
    const err = new Error(why);
    err.wait = retryAfter(r);
    throw err;
  }
  // Refused for good: kept, marked, and shown, never silently dropped.
  await store("readwrite", (s) => s.put({ ...entry, failed: why || `Error ${r.status}` }));
  throw new Rejected(why);
}

async function send(key, url, body) {
  const entry = { key, url, body, at: Date.now() };
  await store("readwrite", (s) => s.put(entry));
  try {
    return await deliver(entry);
  } finally {
    showSync();
  }
}

function later(wait) {
  backoff = Math.min(backoff ? backoff * 2 : 2000, 5 * 60 * 1000);
  const delay = wait ?? backoff;
  retryAt = Date.now() + delay;
  setTimeout(() => { if (Date.now() >= retryAt) flush(); }, delay);
}

async function flush() {
  // In the order they were made: keys sort "retract:" before "shot:", and an undo must follow its shot. A retry
  // stops the queue, so nothing passes an earlier write that hasn't landed.
  const entries = (await store("readonly", (s) => s.getAll())).filter((e) => !e.failed).sort((a, b) => a.at - b.at);
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
  const all = await store("readonly", (s) => s.getAll());
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
    tick();
  });
  rating.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    pressOne(rating, b);
    hint(form, "");
    tick();
  });
  form.querySelector("[data-same]")?.addEventListener("click", () => {
    const last = p.same_as_last;
    pressOne(taste, last.taste ? taste.querySelector(`[data-v="${last.taste}"]`) : null);
    pressOne(rating, rating.querySelector(`[data-v="${last.rating}"]`));
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
      hint(form, err instanceof Rejected ? err.message : OFFLINE);
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
  strip.querySelector("i").addEventListener("animationend", () => { strip.hidden = true; });
  strip.querySelector("button").addEventListener("click", async () => {
    try {
      await send(`retract:${id}`, `/api/shots/${id}/retract`, {});
      location.href = "/";
    } catch (err) {
      strip.firstChild.textContent = err instanceof Rejected ? err.message : OFFLINE;
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
      hint(form, err instanceof Rejected ? err.message : OFFLINE);
    }
  });
}

function equipment(form) {
  const choice = form.querySelector("[data-choice]");
  choice?.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (b) pressOne(choice, b);
  });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = { equipment_id: uuid(), kind: form.dataset.equipment };
    for (const el of form.querySelectorAll("input")) {
      const v = el.value.trim();
      if (!v) return hint(form, "Fill in every field.");
      body[el.name] = el.inputMode === "decimal" ? Number(v.replace(",", ".")) : v;
    }
    if (choice) body[choice.dataset.choice] = pressed(choice) === "true";
    try {
      await send(`equipment:${body.equipment_id}`, "/api/equipment", body);
      location.reload();
    } catch (err) {
      hint(form, err instanceof Rejected ? err.message : OFFLINE);
    }
  });
}

document.addEventListener("DOMContentLoaded", () => {
  const shot = document.getElementById("shot");
  if (shot) logShot(shot);
  const strip = document.querySelector("[data-undo]");
  if (strip) undo(strip);
  const bag = document.getElementById("bag");
  if (bag) newBag(bag);
  for (const f of document.querySelectorAll("[data-equipment]")) equipment(f);
  flush();
});
addEventListener("online", flush);
