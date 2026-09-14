(function () {
  if (window.gamepile) return;
  function token() { return document.querySelector('meta[name="gamepile-token"]')?.content || ''; }
  function showError(message) {
    var notice = document.getElementById('app-error');
    if (!notice) {
      notice = document.createElement('div');
      notice.id = 'app-error';
      notice.className = 'settings-error';
      notice.setAttribute('role', 'alert');
      document.querySelector('main')?.prepend(notice);
    }
    notice.textContent = message;
  }
  window.gamepile = {token: token, showError: showError};
  document.addEventListener('htmx:configRequest', function (event) {
    event.detail.headers['X-GamePile-Token'] = token();
  });
  document.addEventListener('htmx:responseError', function (event) {
    var xhr = event.detail.xhr;
    var message = 'That change could not be completed. Please try again.';
    if (xhr.status < 500) {
      try {
        var detail = JSON.parse(xhr.responseText).detail;
        if (typeof detail === 'string') message = detail;
      } catch (_) {
        if (xhr.responseText.length < 300 && !xhr.responseText.includes('<')) message = xhr.responseText;
      }
    }
    showError(message);
  });
  document.addEventListener('htmx:sendError', function () {
    showError('GamePile could not be reached. Please try again.');
  });
  // Remove the previous error before attempting another action.
  document.addEventListener('htmx:beforeRequest', function () {
    document.getElementById('app-error')?.remove();
  });
})();
