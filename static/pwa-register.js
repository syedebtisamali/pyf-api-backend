// Registers the service worker so the site becomes installable.
// Include this on every page (via <script src="/static/pwa-register.js" defer></script>).
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch((err) => {
      console.warn("Service worker registration failed:", err);
    });
  });
}
