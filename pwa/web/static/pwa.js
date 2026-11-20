// Small behaviours on top of htmx.  No build step, no dependencies.
(function () {
  "use strict";

  // Every htmx request carries the CSRF token.
  document.addEventListener("htmx:configRequest", function (e) {
    e.detail.headers["X-CSRF-Token"] = document.body.dataset.csrf;
  });

  // Pickers: choosing a suggestion fills the hidden id and the visible text.
  document.addEventListener("click", function (e) {
    var pick = e.target.closest(".suggestions .pick");
    if (pick) {
      var picker = pick.closest(".picker");
      picker.querySelector("input[type=hidden]").value = pick.dataset.id;
      picker.querySelector("input[type=search]").value = pick.dataset.label;
      picker.querySelector(".suggestions").innerHTML = "";
      return;
    }
    // Clicking elsewhere closes any open suggestion list.
    document.querySelectorAll(".picker .suggestions").forEach(function (box) {
      if (!box.parentNode.contains(e.target)) box.innerHTML = "";
    });
  });

  // Typing in a picker invalidates the earlier choice until a new one is made.
  document.addEventListener("input", function (e) {
    var picker = e.target.closest(".picker");
    if (picker && e.target.type === "search") {
      picker.querySelector("input[type=hidden]").value = "";
    }
  });

  // Arrow keys move through suggestions; Escape closes them.
  document.addEventListener("keydown", function (e) {
    var picker = e.target.closest && e.target.closest(".picker");
    if (!picker) return;
    var items = Array.prototype.slice.call(picker.querySelectorAll(".pick"));
    if (e.key === "Escape") { picker.querySelector(".suggestions").innerHTML = ""; return; }
    if (!items.length || (e.key !== "ArrowDown" && e.key !== "ArrowUp")) return;
    e.preventDefault();
    var i = items.indexOf(document.activeElement);
    var next = e.key === "ArrowDown" ? i + 1 : i - 1;
    if (next < 0) { picker.querySelector("input[type=search]").focus(); return; }
    items[Math.min(next, items.length - 1)].focus();
  });

  // Enter in a picker's text box takes the first suggestion instead of submitting.
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Enter" || !e.target.matches(".picker input[type=search]")) return;
    var picker = e.target.closest(".picker");
    var first = picker.querySelector(".pick");
    if (first && !picker.querySelector("input[type=hidden]").value) { e.preventDefault(); first.click(); }
  });

  // <select data-toggle="#id"> shows that element only for the "disjoint" choice.
  function applyToggle(select) {
    var target = document.querySelector(select.dataset.toggle);
    if (target) target.hidden = select.value !== "disjoint";
  }
  document.addEventListener("change", function (e) {
    if (e.target.matches("select[data-toggle]")) applyToggle(e.target);
  });
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("select[data-toggle]").forEach(applyToggle);
  });

  // <form data-confirm="..."> asks before submitting.
  document.addEventListener("submit", function (e) {
    var msg = e.target.dataset && e.target.dataset.confirm;
    if (msg && !window.confirm(msg)) e.preventDefault();
  });
})();
