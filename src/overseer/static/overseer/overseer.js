/* Overseer dashboard: dependency-free auto refresh and confirm prompts. */
(function () {
  "use strict";

  function refresh(el) {
    var url = el.getAttribute("data-refresh-url") || window.location.href;
    fetch(url, { headers: { "X-Requested-With": "overseer" }, credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.text() : Promise.reject(r.status); })
      .then(function (html) {
        var doc = new DOMParser().parseFromString(html, "text/html");
        var fresh = doc.querySelector('[data-refresh-id="' + el.getAttribute("data-refresh-id") + '"]');
        // No matching panel means we did not get this page back (an expired session
        // redirects to the login page): keep the last render and do not claim freshness.
        if (!fresh) { return Promise.reject("stale"); }
        el.innerHTML = fresh.innerHTML;
        document.querySelectorAll("[data-updated-at]").forEach(function (n) {
          n.textContent = new Date().toLocaleTimeString();
        });
      })
      .catch(function () { /* keep the last good render */ });
  }

  function start() {
    document.querySelectorAll("[data-refresh-id]").forEach(function (el) {
      var every = parseInt(el.getAttribute("data-refresh-every") || "0", 10) * 1000;
      if (every > 0) {
        setInterval(function () {
          if (!document.hidden && !document.body.classList.contains("paused")) { refresh(el); }
        }, every);
      }
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
