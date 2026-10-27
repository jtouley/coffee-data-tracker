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

class Rejected extends Error {} // the server said no (4xx): retrying won't help

async function deliver(entry) {
  const r = await fetch(entry.url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Requested-With": "fetch" },
    body: JSON.stringify(entry.body),
  });
  // Signed out (401) or a server error: keep it and try again. Anything else is final.
  const final = r.status !== 401 && r.status < 500;
  if (final) await store("readwrite", (s) => s.delete(entry.key));
  if (r.ok) return r.json();
  const detail = (await r.json().catch(() => ({}))).detail;
  if (final) throw new Rejected(Array.isArray(detail) ? detail.join(" ") : detail || r.statusText);
  throw new Error(r.statusText);
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

async function flush() {
  if (!ME) return;
  for (const entry of await mine()) {
    try {
      await deliver(entry);
    } catch (e) {
      if (!(e instanceof Rejected)) break; // offline or server down: try again later
    }
  }
  showSync();
}

async function showSync() {
  const n = (await mine()).length;
  for (const el of document.querySelectorAll("[data-sync]")) {
    if (n) el.textContent = `${n} waiting to sync`;
  }
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
      await post("/api/signin", body);
      location.replace("/");
    } catch (err) {
      hint(form, err instanceof Rejected ? err.message : "Couldn’t reach the server. Check the connection.");
      if (err instanceof Rejected) {
        for (const el of form.querySelectorAll("input")) el.setAttribute("aria-invalid", "true");
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
    b.addEventListener("click", async () => {
      if (b.dataset.confirm && b.textContent !== b.dataset.confirm) {
        b.textContent = b.dataset.confirm; // a second tap, since it signs out every device
        return;
      }
      try {
        await post(b.dataset.signout, {});
      } finally {
        location.replace("/signin");
      }
    });
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
  const signin = document.getElementById("signin");
  if (signin) signIn(signin);
  for (const b of document.querySelectorAll("[data-show]")) showPassword(b);
  account();
  flush();
});
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
addEventListener("online", flush);
