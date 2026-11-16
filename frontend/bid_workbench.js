"use strict";

(function () {
  var started = false;

  function boot() {
    if (started) return;
    started = true;
    import("./current/modules/app.js?v=20260923-assets-2").then(function (module) {
      module.start();
    }).catch(function (error) {
      console.error("Bid evaluation workbench failed to load.", error);
      var loading = document.getElementById("route-loading");
      var title = document.getElementById("route-loading-title");
      if (loading) loading.hidden = false;
      if (title) title.textContent = "The workbench could not be loaded";
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }
})();
