/* FiverrFlow — shared client behaviour.
   Loaded on every page from base.html. */
(function () {
  'use strict';

  // ── Toasts ──────────────────────────────────────────────────────────────
  // Server-rendered flashes are progressively enhanced here; async code calls
  // window.ffToast(msg, category) directly.
  var AUTO_DISMISS_MS = 5000;

  function stack() {
    var el = document.getElementById('toastStack');
    if (!el) {
      el = document.createElement('div');
      el.id = 'toastStack';
      el.className = 'toast-stack';
      el.setAttribute('aria-live', 'polite');
      document.body.appendChild(el);
    }
    return el;
  }

  function dismiss(toast) {
    if (!toast || toast.dataset.closing) return;
    toast.dataset.closing = '1';
    toast.classList.add('ff-toast-out');
    setTimeout(function () { toast.remove(); }, 200);
  }

  function wire(toast) {
    var btn = toast.querySelector('.ff-toast-close');
    if (btn) btn.addEventListener('click', function () { dismiss(toast); });
    if (!toast.classList.contains('ff-toast-sticky')) {
      var timer = setTimeout(function () { dismiss(toast); }, AUTO_DISMISS_MS);
      // Don't yank a message away while it's being read.
      toast.addEventListener('mouseenter', function () { clearTimeout(timer); });
    }
  }

  window.ffToast = function (message, category) {
    var toast = document.createElement('div');
    toast.className = 'ff-toast ff-toast-' + (category || 'info');
    toast.setAttribute('role', 'alert');
    var body = document.createElement('div');
    body.className = 'ff-toast-body';
    body.textContent = message;          // textContent: never inject markup
    var close = document.createElement('button');
    close.type = 'button';
    close.className = 'ff-toast-close';
    close.setAttribute('aria-label', 'Dismiss');
    close.innerHTML = '&times;';
    toast.appendChild(body);
    toast.appendChild(close);
    stack().appendChild(toast);
    wire(toast);
    return toast;
  };

  // ── Theme ───────────────────────────────────────────────────────────────
  // The pre-paint script in <head> sets the initial value; this only handles
  // the toggle and persistence.
  var THEME_KEY = 'ff-theme';

  function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    document.querySelectorAll('[data-theme-toggle]').forEach(function (btn) {
      var isDark = theme === 'dark';
      btn.setAttribute('aria-pressed', String(isDark));
      btn.setAttribute('title', isDark ? 'Switch to light mode' : 'Switch to dark mode');
      var icon = btn.querySelector('i');
      if (icon) icon.className = isDark ? 'bi bi-sun' : 'bi bi-moon-stars';
    });
  }

  function initTheme() {
    var current = document.documentElement.getAttribute('data-theme') || 'dark';
    applyTheme(current);
    document.querySelectorAll('[data-theme-toggle]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
        try { localStorage.setItem(THEME_KEY, next); } catch (_) {}
        applyTheme(next);
      });
    });
  }

  // ── Mobile sidebar ──────────────────────────────────────────────────────
  function initSidebar() {
    var sidebar = document.getElementById('sidebar');
    if (!sidebar) return;
    var backdrop = document.getElementById('sidebarBackdrop');

    function open() {
      sidebar.classList.add('open');
      if (backdrop) backdrop.classList.add('show');
      document.body.classList.add('sidebar-open');
    }
    function close() {
      sidebar.classList.remove('open');
      if (backdrop) backdrop.classList.remove('show');
      document.body.classList.remove('sidebar-open');
    }
    function toggle() {
      sidebar.classList.contains('open') ? close() : open();
    }

    document.querySelectorAll('[data-sidebar-toggle]').forEach(function (btn) {
      btn.addEventListener('click', toggle);
    });
    if (backdrop) backdrop.addEventListener('click', close);
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && sidebar.classList.contains('open')) close();
    });
    // Navigating away should not leave the drawer open behind the new page.
    sidebar.querySelectorAll('a[href]').forEach(function (a) {
      a.addEventListener('click', close);
    });
  }

  // ── Submit guards ───────────────────────────────────────────────────────
  // Prevents the double-submit that created duplicate rows.
  function initSubmitGuards() {
    document.querySelectorAll('form[method="post"], form[method="POST"]').forEach(function (form) {
      form.addEventListener('submit', function () {
        var btn = form.querySelector('[type="submit"]');
        if (!btn || btn.dataset.noGuard) return;
        setTimeout(function () {
          btn.disabled = true;
          if (!btn.querySelector('.spinner-border')) {
            btn.dataset.label = btn.innerHTML;
            btn.innerHTML =
              '<span class="spinner-border spinner-border-sm me-1" aria-hidden="true"></span>Saving…';
          }
        }, 0);
      });
    });
  }

  // ── Confirm dialogs ─────────────────────────────────────────────────────
  // Replaces native confirm() with a Bootstrap modal that names the record.
  function initConfirms() {
    var modalEl = document.getElementById('ffConfirmModal');
    if (!modalEl || typeof bootstrap === 'undefined') return;
    var modal = new bootstrap.Modal(modalEl);
    var msgEl = modalEl.querySelector('[data-confirm-message]');
    var okBtn = modalEl.querySelector('[data-confirm-ok]');
    var pending = null;

    document.querySelectorAll('form[data-confirm]').forEach(function (form) {
      form.addEventListener('submit', function (e) {
        if (form.dataset.confirmed) return;
        e.preventDefault();
        pending = form;
        if (msgEl) msgEl.textContent = form.dataset.confirm;
        modal.show();
      });
    });

    if (okBtn) {
      okBtn.addEventListener('click', function () {
        if (!pending) return;
        pending.dataset.confirmed = '1';
        modal.hide();
        pending.submit();
        pending = null;
      });
    }
  }

  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('#toastStack .ff-toast').forEach(wire);
    initTheme();
    initSidebar();
    initSubmitGuards();
    initConfirms();
  });
})();
