/* Browser-local appearance only. Runs before the stylesheet to restore the
 * palette before first paint; it never reads or writes conversation state. */
(() => {
  "use strict";
  const KEY = "himari.appearance.theme";
  const THEMES = new Set(["classic", "warm"]);
  const normalize = (value) => THEMES.has(value) ? value : "classic";
  let current = "classic";
  try {
    current = normalize(window.localStorage.getItem(KEY));
  } catch (_) {
    // Storage may be disabled. Theme changes still work for this page.
  }
  document.documentElement.dataset.theme = current;

  document.addEventListener("DOMContentLoaded", () => {
    const dialog = document.getElementById("appearance-dialog");
    const feedback = document.getElementById("appearance-feedback");
    const choices = dialog.querySelectorAll('input[name="appearance-theme"]');
    function apply(theme) {
      current = normalize(theme);
      document.documentElement.dataset.theme = current;
      for (const choice of choices) choice.checked = choice.value === current;
    }
    apply(current);
    document.getElementById("nav-appearance").addEventListener("click", () => {
      if (!dialog.open) dialog.showModal();
    });
    document.getElementById("appearance-close").addEventListener("click", () => dialog.close());
    dialog.addEventListener("click", (event) => {
      if (event.target !== dialog) return;
      const bounds = dialog.getBoundingClientRect();
      if (event.clientX < bounds.left || event.clientX > bounds.right
          || event.clientY < bounds.top || event.clientY > bounds.bottom) dialog.close();
    });
    for (const choice of choices) choice.addEventListener("change", () => {
      if (!choice.checked) return;
      apply(choice.value);
      try {
        window.localStorage.setItem(KEY, current);
        feedback.textContent = "Theme saved in this browser.";
      } catch (_) {
        feedback.textContent = "Theme applied for this page. Browser storage is unavailable, so it could not be saved.";
      }
    });
    window.addEventListener("storage", (event) => {
      if (event.key !== KEY && event.key !== null) return;
      // Re-read local storage rather than trusting events for sessionStorage.
      try {
        apply(window.localStorage.getItem(KEY));
        feedback.textContent = "Theme updated from this browser's settings.";
      } catch (_) {
        // Keep the current appearance if storage becomes unavailable.
      }
    });
  });
})();
