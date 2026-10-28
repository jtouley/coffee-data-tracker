  // Theme toggle: remembered per viewer; storage may be unavailable.
  const root = document.documentElement, btn = document.getElementById("theme");
  // The button reads "Dark" and is pressed while the page is dark, whichever way it got there.
  const isDark = () => root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  const sync = () => btn.setAttribute("aria-pressed", isDark());
  try { const t = localStorage.getItem("theme"); if (t) root.dataset.theme = t; } catch (e) {}
  sync();
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", sync);
  btn.addEventListener("click", () => {
    root.dataset.theme = isDark() ? "light" : "dark";
    try { localStorage.setItem("theme", root.dataset.theme); } catch (e) {}
    sync();
  });

  // Tooltips: hover and keyboard focus, textContent only.
  const tip = document.getElementById("tip");
  const show = (el, x, y) => { tip.textContent = el.dataset.tip; tip.style.display = "block";
    tip.style.left = Math.min(x + 12, innerWidth - tip.offsetWidth - 8) + "px"; tip.style.top = (y + 12) + "px"; };
  document.querySelectorAll(".mark").forEach(el => {
    el.addEventListener("pointermove", e => show(el, e.clientX, e.clientY));
    el.addEventListener("pointerleave", () => tip.style.display = "none");
    el.addEventListener("focus", () => { const r = el.getBoundingClientRect(); show(el, r.left, r.top); });
    el.addEventListener("blur", () => tip.style.display = "none");
  });
