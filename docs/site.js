// Shared behaviour for tryinferrail.com pages: the mobile menu, copy
// buttons, links that open a <details> section, and tab sets. No
// dependencies; every page works (minus these conveniences) without it.
(() => {
  'use strict';

  const copyText = async (text) => {
    if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); return; }
    const area = document.createElement('textarea');
    area.value = text; area.setAttribute('readonly', ''); area.style.position = 'fixed'; area.style.opacity = '0';
    document.body.appendChild(area); area.select(); document.execCommand('copy'); area.remove();
  };
  // Exposed for page scripts (docs/try/) so there is one copy routine.
  window.inferrailCopy = copyText;

  const flash = (btn, ok) => {
    const original = btn.dataset.label || btn.textContent;
    btn.dataset.label = original;
    btn.textContent = ok ? 'Copied' : 'Copy failed';
    btn.classList.toggle('copied', ok);
    clearTimeout(btn._copyTimer);
    btn._copyTimer = setTimeout(() => { btn.textContent = original; btn.classList.remove('copied'); }, 1600);
  };
  window.inferrailFlashCopy = flash;

  // Copy buttons: either `data-copy-target="<id>"`, or the <code> inside
  // the same .cmd / .code-block box.
  document.addEventListener('click', async (ev) => {
    const btn = ev.target.closest('button.copy');
    if (!btn) return;
    const targetId = btn.getAttribute('data-copy-target');
    const box = btn.closest('.cmd, .code-block');
    const code = targetId ? document.getElementById(targetId) : box && box.querySelector('code');
    if (!code) return;
    try { await copyText(code.textContent); flash(btn, true); } catch (_e) { flash(btn, false); }
  });

  // Mobile menu.
  const toggle = document.querySelector('.nav-toggle');
  const nav = document.getElementById('primary-nav');
  if (toggle && nav) {
    toggle.addEventListener('click', () => {
      const open = toggle.getAttribute('aria-expanded') !== 'true';
      toggle.setAttribute('aria-expanded', String(open));
      nav.classList.toggle('open', open);
    });
    nav.addEventListener('click', (ev) => {
      if (ev.target.closest('a')) { toggle.setAttribute('aria-expanded', 'false'); nav.classList.remove('open'); }
    });
  }

  // A link with data-open="<details id>" opens that section before the
  // browser scrolls to it.
  const openDetails = (id) => {
    const el = document.getElementById(id);
    if (el && el.tagName === 'DETAILS') el.open = true;
  };
  document.addEventListener('click', (ev) => {
    const link = ev.target.closest('[data-open]');
    if (link) openDetails(link.getAttribute('data-open'));
  });
  if (location.hash) openDetails(location.hash.slice(1));

  // Tabs: .tabs > [role=tablist] > [role=tab][aria-controls]. Arrow keys
  // move between tabs (WAI-ARIA tabs pattern, automatic activation).
  document.querySelectorAll('.tabs').forEach((set) => {
    const tabs = Array.from(set.querySelectorAll('[role="tab"]'));
    const select = (tab, focus) => {
      tabs.forEach((t) => {
        const on = t === tab;
        t.setAttribute('aria-selected', String(on));
        t.tabIndex = on ? 0 : -1;
        const panel = document.getElementById(t.getAttribute('aria-controls'));
        if (panel) panel.hidden = !on;
      });
      if (focus) tab.focus();
    };
    tabs.forEach((tab, i) => {
      tab.addEventListener('click', () => select(tab, false));
      tab.addEventListener('keydown', (ev) => {
        let next = null;
        if (ev.key === 'ArrowRight') next = tabs[(i + 1) % tabs.length];
        if (ev.key === 'ArrowLeft') next = tabs[(i - 1 + tabs.length) % tabs.length];
        if (ev.key === 'Home') next = tabs[0];
        if (ev.key === 'End') next = tabs[tabs.length - 1];
        if (next) { ev.preventDefault(); select(next, true); }
      });
    });
    const initial = tabs.find((t) => t.getAttribute('aria-selected') === 'true') || tabs[0];
    if (initial) select(initial, false);
  });
})();
