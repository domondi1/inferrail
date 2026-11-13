// God Mode freshness: poll the event head and reload when new activity is recorded.
// Same-origin only; the session cookie authenticates the request. No data is cached here.
(function () {
  var seen = document.body.getAttribute("data-seq");
  if (seen === null) return;
  var busy = function () {
    var el = document.activeElement;
    return el && (el.tagName === "INPUT" || el.tagName === "SELECT" || el.tagName === "TEXTAREA");
  };
  var notice = function () {
    if (document.querySelector(".fresh")) return;
    var n = document.createElement("div");
    n.className = "fresh";
    n.textContent = "New activity recorded. Refreshing…";
    document.body.appendChild(n);
  };
  var tick = function () {
    if (document.visibilityState !== "visible") return;
    fetch("/ops/api/summary", { credentials: "same-origin", cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d || String(d.head_seq) === seen) return;
        notice();
        if (!busy()) setTimeout(function () { location.reload(); }, 800);
      })
      .catch(function () {});
  };
  setInterval(tick, 15000);
})();
