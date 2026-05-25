(function () {
  function initRating() {
    var w = document.getElementById("rating-widget");
    if (!w || w.dataset.bound) return;
    w.dataset.bound = "1";
    var appid = w.getAttribute("data-appid");

    w.addEventListener("click", function (e) {
      var clearBtn = e.target.closest("[data-action='clear-rating']");
      if (clearBtn) {
        postRating(appid, null);
        return;
      }
      var half = e.target.closest(".rating-star-half");
      if (!half) return;
      var wrap = half.closest(".rating-star-wrap");
      var isLeft = half.classList.contains("rating-star-half--left");
      var val = parseInt(
        isLeft ? wrap.getAttribute("data-half") : wrap.getAttribute("data-full")
      );
      var current = parseInt(w.getAttribute("data-value")) || 0;
      if (val === current) {
        postRating(appid, null);
        return;
      }
      postRating(appid, val);
    });
  }

  function postRating(appid, val) {
    var body = val !== null ? "rating=" + val : "clear=1";
    fetch("/games/" + appid + "/rating", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: body,
    })
      .then(function (r) {
        return r.text();
      })
      .then(function (html) {
        var container = document.getElementById("game-detail-rating");
        if (container) {
          container.outerHTML = html;
          initRating();
        }
      });
  }

  initRating();
})();
