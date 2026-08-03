/* FiverrFlow — shared client behaviour.
   Loaded on every page from base.html. */
(function () {
  'use strict';

  var REDUCED = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

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
  // the toggle, persistence, and a smooth cross-fade on the <html> element.
  var THEME_KEY = 'ff-theme';

  function applyTheme(theme) {
    var root = document.documentElement;
    root.classList.add('theme-switching');
    root.setAttribute('data-theme', theme);
    try { localStorage.setItem(THEME_KEY, theme); } catch (_) {}
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = theme === 'dark' ? '#0b0d12' : '#f4f6fa';
    document.querySelectorAll('[data-theme-toggle]').forEach(function (btn) {
      var isDark = theme === 'dark';
      btn.setAttribute('aria-pressed', String(isDark));
      btn.setAttribute('title', isDark ? 'Switch to light mode' : 'Switch to dark mode');
      var icon = btn.querySelector('i');
      if (icon) {
        icon.classList.remove('bi-sun', 'bi-moon-stars');
        icon.classList.add(isDark ? 'bi-sun' : 'bi-moon-stars');
        // Re-trigger the icon spin
        icon.style.animation = 'none';
        void icon.offsetWidth;
        icon.style.animation = '';
      }
    });
    setTimeout(function () { root.classList.remove('theme-switching'); }, 320);
  }

  function initTheme() {
    applyTheme(document.documentElement.getAttribute('data-theme') || 'dark');
    document.querySelectorAll('[data-theme-toggle]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
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
      document.querySelector('[data-sidebar-toggle]')?.setAttribute('aria-expanded', 'true');
    }
    function close() {
      sidebar.classList.remove('open');
      if (backdrop) backdrop.classList.remove('show');
      document.body.classList.remove('sidebar-open');
      document.querySelector('[data-sidebar-toggle]')?.setAttribute('aria-expanded', 'false');
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

  // ── Reveal on scroll ────────────────────────────────────────────────────
  // Elements with .reveal fade/rise into view once as they enter the viewport.
  function initReveal() {
    var els = document.querySelectorAll('.reveal');
    if (!els.length) return;
    if (REDUCED || !('IntersectionObserver' in window)) {
      els.forEach(function (el) { el.classList.add('visible'); });
      return;
    }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add('visible');
          io.unobserve(entry.target);
        }
      });
    }, { threshold: 0.12 });
    els.forEach(function (el) { io.observe(el); });
  }

  // ── Animated status bars ────────────────────────────────────────────────
  function initStatusBars() {
    if (REDUCED) {
      document.querySelectorAll('.status-bar').forEach(function (b) { b.style.width = b.dataset.width + '%'; });
      return;
    }
    // Bars render at width:0 and animate to their data-width once painted.
    requestAnimationFrame(function () {
      document.querySelectorAll('.status-bar').forEach(function (bar) {
        bar.style.width = (bar.dataset.width || 0) + '%';
      });
    });
  }

  // ── Count-up numbers ────────────────────────────────────────────────────
  // [data-count="6000" data-prefix="$"] animates 0 → 6,000 when scrolled into view.
  function formatNumber(n, decimals) {
    return n.toLocaleString('en-US', {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals
    });
  }

  function initCountUp() {
    var els = document.querySelectorAll('[data-count]');
    if (!els.length) return;
    var run = function (el) {
      var target = parseFloat(el.dataset.count);
      if (isNaN(target)) { el.textContent = el.dataset.count; return; }
      if (REDUCED) { el.textContent = (el.dataset.prefix || '') + formatNumber(target, el.dataset.decimals || 0); return; }
      var prefix = el.dataset.prefix || '';
      var decimals = parseInt(el.dataset.decimals || '0', 10);
      var duration = 900;
      var start = null;
      function frame(ts) {
        if (!start) start = ts;
        var p = Math.min((ts - start) / duration, 1);
        var eased = 1 - Math.pow(1 - p, 3);            // ease-out cubic
        var val = target * eased;
        el.textContent = prefix + formatNumber(val, decimals);
        if (p < 1) requestAnimationFrame(frame);
        else el.textContent = prefix + formatNumber(target, decimals);
      }
      requestAnimationFrame(frame);
    };

    if (REDUCED || !('IntersectionObserver' in window)) {
      els.forEach(run);
      return;
    }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          run(entry.target);
          io.unobserve(entry.target);
        }
      });
    }, { threshold: 0.4 });
    els.forEach(function (el) { io.observe(el); });
  }

  // ── Relative timestamps ─────────────────────────────────────────────────
  // [data-relative-time="2026-08-03T10:00:00"] renders "3h ago" etc.
  function initRelativeTime() {
    var els = document.querySelectorAll('[data-relative-time]');
    if (!els.length) return;
    function fmt(date) {
      var diff = (Date.now() - date.getTime()) / 1000;
      if (diff < 0) return 'just now';
      if (diff < 60) return 'just now';
      var mins = diff / 60;
      if (mins < 60) return Math.floor(mins) + 'm ago';
      var hrs = mins / 60;
      if (hrs < 24) return Math.floor(hrs) + 'h ago';
      var days = hrs / 24;
      if (days < 7) return Math.floor(days) + 'd ago';
      return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
    }
    els.forEach(function (el) {
      var raw = el.getAttribute('data-relative-time');
      if (!raw) return;
      var d = new Date(raw);
      if (isNaN(d.getTime())) return;
      el.textContent = fmt(d);
      setInterval(function () { el.textContent = fmt(d); }, 60000);
    });
  }

  // ── Page load ───────────────────────────────────────────────────────────
  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('#toastStack .ff-toast').forEach(wire);
    initTheme();
    initSidebar();
    initSubmitGuards();
    initConfirms();
    initReveal();
    initStatusBars();
    initCountUp();
    initRelativeTime();
  });
})();
