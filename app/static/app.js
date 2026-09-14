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
    showError(event.detail.xhr.status === 403
      ? 'Your session has expired. Reload GamePile and try again.'
      : 'That change could not be completed. Reload the page and try again.');
  });
  document.addEventListener('htmx:sendError', function () {
    showError('GamePile could not be reached. Please try again.');
  });
  // One request per action surface until the current response arrives.
  document.addEventListener('htmx:beforeRequest', function () {
    document.getElementById('app-error')?.remove();
  });
})();
