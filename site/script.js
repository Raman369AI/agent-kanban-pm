(function () {
  "use strict";

  var header = document.querySelector("[data-header]");
  var toast = document.querySelector("[data-toast]");
  var toastTimer;

  function updateHeader() {
    if (header) header.classList.toggle("is-scrolled", window.scrollY > 12);
  }

  function showToast(message) {
    if (!toast) return;
    toast.textContent = message;
    toast.classList.add("is-visible");
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(function () {
      toast.classList.remove("is-visible");
    }, 1800);
  }

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text);
    }
    var input = document.createElement("textarea");
    input.value = text;
    input.setAttribute("readonly", "");
    input.style.position = "fixed";
    input.style.opacity = "0";
    document.body.appendChild(input);
    input.select();
    document.execCommand("copy");
    input.remove();
    return Promise.resolve();
  }

  document.querySelectorAll("[data-copy-target]").forEach(function (button) {
    button.addEventListener("click", function () {
      var target = document.getElementById(button.dataset.copyTarget);
      if (!target) return;
      var command = target.innerText
        .split("\n")
        .filter(function (line) { return !line.includes("Running at"); })
        .map(function (line) { return line.replace(/^\$\s*/, ""); })
        .join("\n")
        .trim();
      copyText(command).then(function () {
        var previous = button.textContent;
        button.textContent = "Copied";
        showToast("Install commands copied");
        window.setTimeout(function () { button.textContent = previous; }, 1600);
      }).catch(function () {
        showToast("Copy failed — select the commands manually");
      });
    });
  });

  var revealItems = Array.from(document.querySelectorAll("[data-reveal]"));
  if ("IntersectionObserver" in window) {
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        entry.target.classList.add("is-visible");
        observer.unobserve(entry.target);
      });
    }, { threshold: 0.12, rootMargin: "0px 0px -40px" });
    revealItems.forEach(function (item) { observer.observe(item); });
  } else {
    revealItems.forEach(function (item) { item.classList.add("is-visible"); });
  }

  window.addEventListener("scroll", updateHeader, { passive: true });
  updateHeader();
}());
