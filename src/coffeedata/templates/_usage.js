  // Counting visits, searches, filters and clicks to roasters (privacy policy, "What we count"). Nothing is read from
  // or stored in the browser, no id is made, and nothing is sent with Global Privacy Control or Do Not Track on.
  // Events wait in memory and leave on a click to a roaster or when the page is hidden, never while it draws; they go
  // to a separate host without credentials, and any failure is dropped.
  const usage = (() => {
    const cfg = {{ usage | tojson }};  // {url, surface, release}
    const off = navigator.globalPrivacyControl === true || [navigator.doNotTrack, window.doNotTrack].includes("1");
    const queue = [], hooks = [];
    const uid = () => crypto.randomUUID ? crypto.randomUUID()
      : [...crypto.getRandomValues(new Uint8Array(16))].map(b => b.toString(16).padStart(2, "0")).join("");
    // Where this load came from, sent once with its first event (entry): utm_* from the address (then removed from
    // it) and the referrer's host name only, never its path or query. The collector reads the source from them.
    let source = null;
    if (!off) {
      const p = new URLSearchParams(location.search);
      source = {entry: true};
      for (const k of ["utm_source", "utm_medium", "utm_campaign"]) {
        const v = (p.get(k) || "").toLowerCase();
        if (/^[a-z0-9._-]{1,40}$/.test(v)) source[k] = v;
      }
      try { if (document.referrer) source.referrer = new URL(document.referrer).hostname; } catch (e) {}
      const keys = [...p.keys()].filter(k => k.startsWith("utm_"));
      if (keys.length) {
        keys.forEach(k => p.delete(k));
        const s = p.toString();
        try { history.replaceState(history.state, "", location.pathname + (s ? "?" + s : "") + location.hash); } catch (e) {}
      }
    }
    function post(events) {
      const body = JSON.stringify({events});
      try {
        fetch(cfg.url, {method: "POST", body, keepalive: true, credentials: "omit", mode: "no-cors", cache: "no-store",
          referrerPolicy: "strict-origin-when-cross-origin"}).catch(() => {});  // the policy keeps Origin, which the collector needs
      } catch (e) {
        try { navigator.sendBeacon(cfg.url, body); } catch (e2) {}  // no fetch: the beacon (nothing sets cookies on that host)
      }
    }
    function flush() {
      hooks.forEach(f => { try { f(); } catch (e) {} });
      // At most 20 events and 4 KB (in UTF-8) a request, the collector's limits.
      const size = events => new TextEncoder().encode(JSON.stringify({events})).length;
      while (queue.length) {
        let n = Math.min(queue.length, 20);
        while (n > 1 && size(queue.slice(0, n)) > 4000) n--;
        post(queue.splice(0, n));
      }
    }
    function send(fields) {
      if (off) return;
      const e = {event_id: uid(), surface: cfg.surface, release: cfg.release,
        device: matchMedia("(max-width: 600px)").matches ? "phone" : "desktop", webdriver: navigator.webdriver === true, ...fields};
      if (source) { Object.assign(e, source); source = null; }
      queue.push(e);
      if (queue.length >= 20) flush();
    }
    if (!off) {
      addEventListener("pagehide", flush);
      document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") flush(); });
      // A click (or middle click) on a link out to a roaster or coffee: the link carries its ids and, in a result
      // list, its place; the page adds what led there.
      const out = e => {
        const a = e.target.closest && e.target.closest("a[data-out]");
        if (!a || !a.dataset.roaster || (e.type === "auxclick" && e.button !== 1)) return;  // a click_out names its roaster
        const rank = a.dataset.rank ? +a.dataset.rank : null;
        send({...(rank && usage.context ? usage.context() : {}), event: "click_out", roaster_id: a.dataset.roaster,
          coffee_id: a.dataset.coffee || null, rank});
        flush();
      };
      document.addEventListener("click", out, true);
      document.addEventListener("auxclick", out, true);
    }
    send({event: "page_view"});  // waits for the first flush
    return {send, flush, hooks, context: null};
  })();
