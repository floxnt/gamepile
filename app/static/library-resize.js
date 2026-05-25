(function () {
  var STORAGE_KEY = "gamepile-col-widths";
  var MIN_PX = 60;

  function init() {
    var table = document.querySelector(".library-table");
    if (!table) return;

    var cols = table.querySelectorAll("colgroup col");
    var ths = table.querySelectorAll("thead th");
    if (!cols.length) return;

    ensurePxWidths(cols, table);
    restoreWidths(cols, table);

    for (var i = 0; i < ths.length - 1; i++) {
      if (!ths[i].querySelector(".col-resize-handle")) {
        addHandle(ths[i], cols, i, table);
      }
    }
  }

  function ensurePxWidths(cols, table) {
    var first = cols[0].style.width;
    if (first && first.indexOf("%") !== -1) {
      for (var i = 0; i < cols.length; i++) {
        var rect = cols[i].getBoundingClientRect
          ? null
          : null;
        var th = table.querySelectorAll("thead th")[i];
        if (th) {
          cols[i].style.width = th.getBoundingClientRect().width + "px";
        }
      }
    }
  }

  function restoreWidths(cols, table) {
    try {
      var saved = JSON.parse(localStorage.getItem(STORAGE_KEY));
      if (!saved) return;

      var firstId = cols[0].getAttribute("data-col");
      if (!firstId || typeof saved[firstId] !== "number") return;

      var isOldFormat = true;
      for (var i = 0; i < cols.length; i++) {
        var id = cols[i].getAttribute("data-col");
        if (id && typeof saved[id] === "number" && saved[id] > 100) {
          isOldFormat = false;
          break;
        }
      }

      if (isOldFormat) {
        var tableW = table.getBoundingClientRect().width;
        for (var j = 0; j < cols.length; j++) {
          var cid = cols[j].getAttribute("data-col");
          if (cid && typeof saved[cid] === "number") {
            cols[j].style.width = (saved[cid] / 100) * tableW + "px";
          }
        }
      } else {
        for (var k = 0; k < cols.length; k++) {
          var kid = cols[k].getAttribute("data-col");
          if (kid && typeof saved[kid] === "number") {
            cols[k].style.width = saved[kid] + "px";
          }
        }
      }
    } catch (e) {}
  }

  function saveWidths(cols) {
    try {
      var w = {};
      for (var i = 0; i < cols.length; i++) {
        var id = cols[i].getAttribute("data-col");
        if (id) w[id] = Math.round(parseFloat(cols[i].style.width));
      }
      localStorage.setItem(STORAGE_KEY, JSON.stringify(w));
    } catch (e) {}
  }

  function addHandle(th, cols, idx, table) {
    var h = document.createElement("div");
    h.className = "col-resize-handle";
    th.appendChild(h);

    h.addEventListener("mousedown", function (e) {
      e.preventDefault();
      e.stopPropagation();

      ensurePxWidths(cols, table);

      var startX = e.clientX;
      var startWidths = [];
      for (var i = 0; i < cols.length; i++) {
        startWidths.push(parseFloat(cols[i].style.width));
      }

      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
      document.body.style.webkitUserSelect = "none";

      function onMove(e) {
        var delta = e.clientX - startX;
        var widths = startWidths.slice();

        if (delta > 0) {
          var remaining = delta;
          for (var r = idx + 1; r < widths.length && remaining > 0; r++) {
            var available = widths[r] - MIN_PX;
            if (available <= 0) continue;
            var take = Math.min(remaining, available);
            widths[r] -= take;
            remaining -= take;
          }
          widths[idx] = startWidths[idx] + (delta - remaining);
        } else {
          var shrink = -delta;
          var maxShrink = startWidths[idx] - MIN_PX;
          if (shrink > maxShrink) shrink = maxShrink;
          widths[idx] = startWidths[idx] - shrink;
          widths[idx + 1] = startWidths[idx + 1] + shrink;
        }

        for (var c = 0; c < cols.length; c++) {
          cols[c].style.width = widths[c] + "px";
        }
      }

      function onUp() {
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        document.body.style.cursor = "";
        document.body.style.userSelect = "";
        document.body.style.webkitUserSelect = "";
        saveWidths(cols);
      }

      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    });
  }

  init();

  document.addEventListener("htmx:afterSettle", function (e) {
    if (
      e.detail &&
      e.detail.target &&
      (e.detail.target.id === "library-content" ||
        e.detail.target.querySelector(".library-table"))
    ) {
      init();
    }
  });
})();
