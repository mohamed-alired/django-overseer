/* Overseer dashboard: dependency-free auto refresh and confirm prompts. */
(function () {
  "use strict";

  function stamp() {
    document.querySelectorAll("[data-updated-at]").forEach(function (n) {
      n.textContent = new Date().toLocaleTimeString();
    });
  }

  // One fetch per (url, interval) group refreshes every region in it: a page with
  // several regions does not ask the server for the same page several times.
  function refresh(url, els) {
    fetch(url, { headers: { "X-Requested-With": "overseer" }, credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.text() : Promise.reject(r.status); })
      .then(function (html) {
        var doc = new DOMParser().parseFromString(html, "text/html");
        var updated = 0;
        els.forEach(function (el) {
          var fresh = doc.querySelector('[data-refresh-id="' + el.getAttribute("data-refresh-id") + '"]');
          // No matching region means we did not get this page back (an expired session
          // redirects to the login page): keep the last render.
          if (fresh) { el.innerHTML = fresh.innerHTML; updated += 1; }
        });
        if (updated) { stamp(); }
      })
      .catch(function () { /* keep the last good render */ });
  }

  function start() {
    var groups = {};
    document.querySelectorAll("[data-refresh-id]").forEach(function (el) {
      var every = parseInt(el.getAttribute("data-refresh-every") || "0", 10) * 1000;
      if (!(every > 0)) { return; }
      var url = el.getAttribute("data-refresh-url") || window.location.href;
      var key = every + " " + url;
      (groups[key] = groups[key] || { url: url, every: every, els: [] }).els.push(el);
    });
    Object.keys(groups).forEach(function (key) {
      var group = groups[key];
      setInterval(function () {
        if (!document.hidden && !document.body.classList.contains("paused")) { refresh(group.url, group.els); }
      }, group.every);
    });
    document.addEventListener("submit", function (e) {
      var form = e.target;
      if (form.hasAttribute("data-confirm") && !window.confirm(form.getAttribute("data-confirm"))) {
        e.preventDefault();
      }
    });
    var toggle = document.getElementById("ov-pause");
    if (toggle) {
      toggle.addEventListener("click", function () {
        document.body.classList.toggle("paused");
        toggle.textContent = document.body.classList.contains("paused") ? "Resume" : "Pause";
      });
    }
  }

  if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", start); } else { start(); }
})();
