(function () {
  var STORAGE_KEY = "gamepile-col-widths";
  var MIN_PX = 60;

  function init() {
    var table = document.querySelector(".library-table");
    if (!table) return;

    var cols = table.querySelectorAll("colgroup col");
    var ths = table.querySelectorAll("thead th");
    if (!cols.length) return;

    restoreWidths(cols);

    for (var i = 0; i < ths.length - 1; i++) {
      addHandle(ths[i], cols, i, table);
    }
  }

  function restoreWidths(cols) {
    try {
      var saved = JSON.parse(localStorage.getItem(STORAGE_KEY));
      if (!saved) return;
      for (var i = 0; i < cols.length; i++) {
        var id = cols[i].getAttribute("data-col");
        if (id && typeof saved[id] === "number") {
          cols[i].style.width = saved[id] + "%";
        }
      }
    } catch (e) {}
  }

  function saveWidths(cols) {
    try {
      var w = {};
      for (var i = 0; i < cols.length; i++) {
        var id = cols[i].getAttribute("data-col");
        if (id) w[id] = parseFloat(cols[i].style.width);
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

      var startX = e.clientX;
      var tableW = table.getBoundingClientRect().width;
      var startPct = parseFloat(cols[idx].style.width);
      var nextPct = parseFloat(cols[idx + 1].style.width);
      var minPct = (MIN_PX / tableW) * 100;

      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
      document.body.style.webkitUserSelect = "none";

      function onMove(e) {
        var delta = ((e.clientX - startX) / tableW) * 100;
        var newPct = startPct + delta;
        var newNext = nextPct - delta;

        if (newPct < minPct) {
          newPct = minPct;
          newNext = startPct + nextPct - minPct;
        }
        if (newNext < minPct) {
          newNext = minPct;
          newPct = startPct + nextPct - minPct;
        }

        cols[idx].style.width = newPct + "%";
        cols[idx + 1].style.width = newNext + "%";
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
