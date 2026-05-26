(function () {
  var STORAGE_KEY = "gamepile-col-widths";
  var MIN_PX = 60;
  var naturalWidths = null;

  function init() {
    var table = document.querySelector(".library-table");
    if (!table) return;
    var cols = table.querySelectorAll("colgroup col");
    var ths = table.querySelectorAll("thead th");
    if (!cols.length) return;

    ensurePxWidths(cols, table);
    loadNaturalWidths(cols, table);

    for (var i = 0; i < ths.length - 1; i++) {
      if (!ths[i].querySelector(".col-resize-handle")) {
        addHandle(ths[i], cols, i, table);
      }
    }
  }

  function ensurePxWidths(cols, table) {
    var first = cols[0].style.width;
    if (!first || first.indexOf("%") !== -1) {
      var ths = table.querySelectorAll("thead th");
      for (var i = 0; i < cols.length; i++) {
        if (ths[i]) {
          cols[i].style.width = ths[i].getBoundingClientRect().width + "px";
        }
      }
    }
  }

  function loadNaturalWidths(cols, table) {
    try {
      var saved = JSON.parse(localStorage.getItem(STORAGE_KEY));
      if (saved) {
        var firstId = cols[0].getAttribute("data-col");
        if (firstId && typeof saved[firstId] === "number") {
          naturalWidths = [];
          var isOldPct = true;
          for (var i = 0; i < cols.length; i++) {
            var id = cols[i].getAttribute("data-col");
            if (id && typeof saved[id] === "number" && saved[id] > 100) {
              isOldPct = false;
              break;
            }
          }
          var tableW = table.getBoundingClientRect().width;
          for (var j = 0; j < cols.length; j++) {
            var jid = cols[j].getAttribute("data-col");
            if (jid && typeof saved[jid] === "number") {
              naturalWidths.push(isOldPct ? (saved[jid] / 100) * tableW : saved[jid]);
            } else {
              naturalWidths.push(parseFloat(cols[j].style.width));
            }
          }
          for (var k = 0; k < cols.length; k++) {
            cols[k].style.width = naturalWidths[k] + "px";
          }
          return;
        }
      }
    } catch (e) {}
    naturalWidths = [];
    for (var m = 0; m < cols.length; m++) {
      naturalWidths.push(parseFloat(cols[m].style.width));
    }
  }

  function saveNaturalWidths(cols) {
    if (!naturalWidths) return;
    try {
      var w = {};
      for (var i = 0; i < cols.length; i++) {
        var id = cols[i].getAttribute("data-col");
        if (id) w[id] = Math.round(naturalWidths[i]);
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
      var startW = [];
      for (var i = 0; i < cols.length; i++) {
        startW.push(parseFloat(cols[i].style.width));
      }
      var snapNat = naturalWidths.slice();

      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
      document.body.style.webkitUserSelect = "none";

      function onMove(e) {
        var delta = e.clientX - startX;
        var w = startW.slice();

        if (delta > 0) {
          var need = delta;
          for (var r = idx + 1; r < w.length && need > 0; r++) {
            var avail = w[r] - MIN_PX;
            if (avail <= 0) continue;
            var take = Math.min(need, avail);
            w[r] -= take;
            need -= take;
          }
          w[idx] = startW[idx] + (delta - need);
        } else {
          var toFree = -delta;
          var freed = 0;

          var fromIdx = Math.min(toFree, Math.max(0, startW[idx] - MIN_PX));
          w[idx] = startW[idx] - fromIdx;
          freed += fromIdx;
          toFree -= fromIdx;

          for (var l = idx - 1; l >= 0 && toFree > 0; l--) {
            var la = Math.max(0, w[l] - MIN_PX);
            if (la <= 0) continue;
            var lt = Math.min(toFree, la);
            w[l] -= lt;
            freed += lt;
            toFree -= lt;
          }

          var pool = freed;
          for (var r2 = idx + 1; r2 < w.length && pool > 0; r2++) {
            var room = snapNat[r2] - w[r2];
            if (room <= 0) continue;
            var give = Math.min(pool, room);
            w[r2] += give;
            pool -= give;
          }
          if (pool > 0) {
            w[idx + 1] += pool;
          }
        }

        for (var c = 0; c < cols.length; c++) {
          cols[c].style.width = w[c] + "px";
        }
      }

      function onUp() {
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        document.body.style.cursor = "";
        document.body.style.userSelect = "";
        document.body.style.webkitUserSelect = "";

        naturalWidths[idx] = parseFloat(cols[idx].style.width);
        saveNaturalWidths(cols);
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
