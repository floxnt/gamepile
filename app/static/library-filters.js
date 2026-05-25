(function () {
  var toggle = document.getElementById("filter-toggle");
  var panel = document.getElementById("filter-panel");
  var search = document.getElementById("tag-search");
  var dropdown = document.getElementById("tag-dropdown");
  var chipsEl = document.getElementById("tag-chips");
  var hidden = document.getElementById("tags-hidden");

  if (!toggle || !panel) return;

  // --- Panel toggle ---
  toggle.addEventListener("click", function () {
    panel.hidden = !panel.hidden;
  });

  // --- Tag picker ---
  if (search && dropdown) {
    search.addEventListener("focus", function () {
      dropdown.hidden = false;
      filterOptions("");
    });

    search.addEventListener("input", function () {
      filterOptions(search.value);
    });

    document.addEventListener("click", function (e) {
      if (!e.target.closest("#tag-picker")) {
        dropdown.hidden = true;
        search.value = "";
      }
    });

    dropdown.addEventListener("change", function (e) {
      if (e.target.type === "checkbox") syncTags();
    });
  }

  // --- Chip removal (panel chips + bar chips) ---
  document.addEventListener("click", function (e) {
    var btn = e.target.closest(".chip-remove");
    if (!btn) return;
    var tag = btn.getAttribute("data-tag");
    if (!tag) return;

    // Uncheck in dropdown
    if (dropdown) {
      var cbs = dropdown.querySelectorAll('input[type="checkbox"]');
      for (var i = 0; i < cbs.length; i++) {
        if (cbs[i].value === tag) cbs[i].checked = false;
      }
    }
    syncTags();

    // If click was on the bar chip (outside panel), auto-submit
    if (e.target.closest("#active-chips")) {
      var form = document.getElementById("filter-form");
      if (form) form.requestSubmit();
    }
  });

  function filterOptions(q) {
    var needle = q.toLowerCase();
    var opts = dropdown.querySelectorAll(".tag-option");
    for (var i = 0; i < opts.length; i++) {
      var text = opts[i].textContent.trim().toLowerCase();
      opts[i].style.display = text.includes(needle) ? "" : "none";
    }
  }

  function syncTags() {
    var cbs = dropdown.querySelectorAll('input[type="checkbox"]:checked');
    var selected = [];
    for (var i = 0; i < cbs.length; i++) selected.push(cbs[i].value);

    hidden.value = selected.join(",");

    // Rebuild chips
    chipsEl.innerHTML = "";
    for (var j = 0; j < selected.length; j++) {
      var span = document.createElement("span");
      span.className = "filter-chip";
      span.textContent = selected[j] + " ";
      var rm = document.createElement("button");
      rm.type = "button";
      rm.className = "chip-remove";
      rm.setAttribute("data-tag", selected[j]);
      rm.textContent = "×";
      span.appendChild(rm);
      chipsEl.appendChild(span);
    }
  }
})();
