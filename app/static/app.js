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
  // A photo waits as a Blob (IndexedDB keeps those); everything else is JSON.
  const r = await fetch(entry.url, {
    method: "POST",
    headers: { "Content-Type": entry.blob ? entry.blob.type || "image/jpeg" : "application/json", "X-Requested-With": "fetch" },
    body: entry.blob || JSON.stringify(entry.body),
  });
  // Signed out (401) or a server error: keep it and try again. Anything else is final.
  const final = r.status !== 401 && r.status < 500;
  if (final) await store("readwrite", (s) => s.delete(entry.key));
  if (r.ok) return r.json();
  const detail = (await r.json().catch(() => ({}))).detail;
  if (final) throw new Rejected(Array.isArray(detail) ? detail.join(" ") : detail || r.statusText);
  throw new Error(r.statusText);
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

let flushing = null;
function flush() {
  // One pass at a time: "online" and a page load can both start one, and two would send each entry twice.
  flushing ??= flushOnce().finally(() => { flushing = null; });
  return flushing;
}

async function flushOnce() {
  if (!ME) return;
  // In the order they were made: keys sort "retract:" before "shot:", and an undo must follow its shot.
  const entries = (await mine()).sort((a, b) => a.at - b.at);
  for (const entry of entries) {
    try {
      await deliver(entry);
    } catch (e) {
      if (!(e instanceof Rejected)) break; // offline or server down: try again later
    }
  }
  showSync();
}

const SYNCED = '<svg viewBox="0 0 12 12" width="12" height="12" aria-hidden="true"><path d="M2 6.5l2.5 2.5L10 3" fill="none" stroke="currentColor" stroke-width="1.75"/></svg>Synced';

async function showSync() {
  const n = (await mine()).length;
  for (const el of document.querySelectorAll("[data-sync]")) {
    if (n) el.textContent = `${n} waiting to sync`;
    else if ("quiet" in el.dataset) el.textContent = ""; // where nothing was waiting, nothing to say
    else el.innerHTML = SYNCED;
  }
  if (!n) document.dispatchEvent(new Event("synced"));
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
    if (time === null) {
      if (p.last_time_s == null) return hint(form, "Time the shot, or type the time.");
      if (!confirmLastTime) {
        confirmLastTime = true; // a forgotten timer costs one more tap, never a made-up time
        save.textContent = `Save with ${p.last_time_s} s?`;
        return;
      }
      time = p.last_time_s;
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
    save.disabled = true;
    try {
      await send(`shot:${body.shot_id}`, "/api/shots", body);
      tick(8);
      setTimeout(() => tick(8), 60);
      location.href = fix ? `/shot/${body.shot_id}` : `/saved/${body.shot_id}?new=1`;
    } catch (err) {
      if (err instanceof Rejected) {
        hint(form, err.message);
        save.disabled = false;
        return;
      }
      offlineSaved(body); // it's in the outbox: say so, and don't leave the form armed for a second save
    }
  });

  // Saved with no connection: what was saved, described from the form alone (no server logic copied here).
  function offlineSaved(b) {
    const view = document.querySelector("[data-offline-saved]");
    const o = (k) => view.querySelector(`[data-o="${k}"]`);
    const now = new Date();
    o("title").textContent = `Saved ${now.getHours()}:${String(now.getMinutes()).padStart(2, "0")} on this phone · ${view.dataset.name}`;
    o("ratio").textContent = `1:${(b.yield_g / b.dose_g).toFixed(2)}`;
    o("dose").textContent = `${b.dose_g.toFixed(1)} → ${b.yield_g.toFixed(1)}`;
    o("time").textContent = `${b.time_s ?? "–"}`;
    o("grind").textContent = `Grind ${b.grinder_setting} · ${view.dataset.grinder}`;
    // The same tag as Saved; with no taste, just the rating.
    const tag = o("taste");
    tag.className = b.taste ? `taste ${b.taste}` : "meta";
    tag.textContent = b.taste ? `${b.taste[0].toUpperCase()}${b.taste.slice(1)} · ${b.rating}` : `Rated ${b.rating}`;
    form.hidden = true;
    for (const el of document.querySelectorAll("[data-online-only]")) el.hidden = true;
    view.hidden = false;
    view.querySelector("h1").focus({ preventScroll: true });
    scrollTo(0, 0);
    // Once it syncs, the comparison is a tap away.
    document.addEventListener("synced", () => {
      const link = Object.assign(document.createElement("a"), { className: "ext", href: `/saved/${b.shot_id}`, textContent: "Synced. See how it compares ›" });
      o("summary").replaceChildren(link);
    }, { once: true });
    unqueue(view.querySelector("[data-unqueue]"), b.shot_id);
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
    if (b) pressOne(milk, b);
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
      location.href = "/";
    } catch (err) {
      strip.firstChild.textContent = err instanceof Rejected ? err.message : OFFLINE;
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
    const queued = await store("readonly", (s) => s.get(key));
    try {
      if (queued) await store("readwrite", (s) => s.delete(key));
      else await send(`retract:${id}`, `/api/shots/${id}/retract`, {});
      location.href = "/";
    } catch (err) {
      strip.firstChild.textContent = err instanceof Rejected ? err.message : OFFLINE;
    }
  });
}

// --- New bag and equipment ---------------------------------------------------------------------------

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
  let saving = false;

  const save = async (body, where) => {
    if (saving) return; // a second tap mustn't make a second bag
    saving = true;
    for (const b of root.querySelectorAll("button")) b.disabled = true;
    body = { bag_id: uuid(), ...body };
    if (read) Object.assign(body, { photo_id: read.photo_id, read_json: read.raw });
    if (read?.roast_date && !body.roast_date) body.roast_date = read.roast_date;
    if (read?.label?.weight_g && !body.weight_g) body.weight_g = read.label.weight_g;
    try {
      await send(`bag:${body.bag_id}`, "/api/bags", body);
      location.href = "/log";
    } catch (err) {
      hint(where, err instanceof Rejected ? err.message : OFFLINE);
      if (!(err instanceof Rejected)) return; // queued: it stays saved, so another tap would make a second bag
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
  const days = (iso) => Math.round((Date.now() - new Date(`${iso}T12:00:00`)) / 86400000);
  const showMatch = (m) => {
    fill("title", m.title);
    fill("roaster", m.roaster);
    const d = read.roast_date;
    fill("roasted", d ? `${new Date(`${d}T12:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric" })} · day ${days(d)} · read from the bag` : "");
    fill("origin", m.origin);
    fill("notes", m.notes);
    fill("price", m.price);
    fill("stock", m.in_stock == null ? "" : m.in_stock ? "In stock" : "Sold out");
    root.querySelector("[data-use]").onclick = () => save({ coffee_id: m.coffee_id, coffee_freetext: m.title, match_score: m.score ?? null }, root.querySelector('[data-step="match"]'));
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
      meta.textContent = [sameRoaster ? "" : c.roaster, c.origin, c.in_stock == null ? "" : c.in_stock ? "in stock" : "sold out"]
        .filter(Boolean).join(" · ");
      name.append(meta);
      const score = document.createElement("span");
      score.className = "meta";
      score.textContent = c.score ? `match ${c.score}` : "";
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
    b.onclick = () => (b.dataset.goto === "type" ? typed() : b.dataset.goto === "pick" ? showPick(Boolean(read?.match)) : step(b.dataset.goto));
  }
  input?.addEventListener("change", async () => {
    const file = input.files[0];
    input.value = "";
    if (!file) return;
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
        headers: { "Content-Type": photo.type || "image/jpeg", "X-Requested-With": "fetch" },
        body: photo,
      });
      const body = await r.json().catch(() => ({}));
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
      // Offline: the photo waits in the outbox and goes up with the bag; the server reads it when it arrives.
      await store("readwrite", (s) => s.put({ key: `photo:${id}`, url: `/api/photos/${id}`, body: null, blob: photo, at: Date.now(), user: ME }));
      read = { photo_id: id, raw: null };
      showSync();
      root.querySelector("[data-photo-waiting]").hidden = false;
      step("queued");
    }
  });

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const coffee = form.coffee_freetext.value.trim();
    if (!coffee) return hint(form, "Type the roaster and coffee.");
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
        if (!el.required) continue;
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
      hint(form, err instanceof Rejected ? err.message : OFFLINE);
    }
  }
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    save({});
  });
  const retire = form.querySelector("[data-retire]");
  retire?.addEventListener("click", () => {
    if (retire.textContent !== retire.dataset.confirm) {
      const label = retire.textContent;
      retire.textContent = retire.dataset.confirm; // a second tap: retired gear leaves the pickers
      setTimeout(() => { retire.textContent = label; }, 4000);
      return;
    }
    save({ retired: true });
  });
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
  const fromLink = decodeURIComponent(location.hash.slice(1));
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
  another.onclick = () => {
    if (another.textContent !== another.dataset.confirm) {
      const label = another.textContent;
      another.textContent = another.dataset.confirm;
      setTimeout(() => { another.textContent = label; }, 4000);
      return;
    }
    another.textContent = "Make another link";
    makeOne();
  };
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
    b.onclick = () => {
      if (b.dataset.confirm && b.textContent !== b.dataset.confirm) {
        const label = b.textContent;
        b.textContent = b.dataset.confirm;
        setTimeout(() => { b.textContent = label; }, 4000);
        return;
      }
      act(`/api/people/${b.dataset.person}`, { disabled: b.dataset.disabled === "true" }, () => location.reload());
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
  const leave = () => { timer = setTimeout(() => location.replace(b.dataset.back), 5000); };
  strip.addEventListener("focusin", () => clearTimeout(timer)); // focus on Undo holds it open
  strip.addEventListener("focusout", leave);
  b.addEventListener("click", async () => {
    b.disabled = true;
    try {
      await send(`retract:${id}`, `/api/shots/${id}/retract`, {});
      tick(8);
      actions.hidden = true;
      strip.hidden = false;
      leave();
    } catch (err) {
      hint(document.querySelector("main"), err instanceof Rejected ? err.message : OFFLINE);
      b.disabled = false;
    }
  });
  strip.querySelector("button").addEventListener("click", async () => {
    clearTimeout(timer);
    try {
      await send(`restore:${id}`, `/api/shots/${id}/restore`, {});
      location.reload();
    } catch (err) {
      strip.firstChild.textContent = err instanceof Rejected ? err.message : OFFLINE;
    }
  });
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
    b.addEventListener("click", async () => {
      if (b.dataset.confirm && b.textContent !== b.dataset.confirm) {
        const label = b.textContent;
        b.textContent = b.dataset.confirm; // a second tap, since it signs out every device
        setTimeout(() => { b.textContent = label; }, 4000);
        return;
      }
      try {
        await post(b.dataset.signout, {});
      } finally {
        // Kept pages hold this person's data: they go with the session.
        await caches.delete("pages").catch(() => {});
        navigator.serviceWorker?.controller?.postMessage("signed-out");
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
  const bag = document.getElementById("newbag");
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
