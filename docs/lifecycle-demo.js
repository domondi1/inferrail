// The homepage lifecycle demo: turns the ordered list of six steps into a stepper with a job
// budget and a receipt that fill in as the job runs. Without this script the list, the final
// budget and the full receipt stay readable as they are. Example data only; nothing is paid.
(() => {
  'use strict';
  const root = document.querySelector('[data-lifecycle]');
  if (!root) return;

  const BUDGET = 0.01;
  const steps = Array.from(root.querySelectorAll('.lc-step'));
  const rows = Array.from(root.querySelectorAll('[data-row]'));
  const el = (name) => root.querySelector(`[data-lc="${name}"]`);
  const usd = (n) => `$${n.toFixed(3)}`;
  const meter = el('meter');
  const [spentBar, heldBar] = meter.children;
  let current = 0;

  const show = (index, announce) => {
    current = Math.max(0, Math.min(steps.length - 1, index));
    const step = steps[current];
    const held = Number(step.dataset.held);
    const spent = Number(step.dataset.spent);
    steps.forEach((s, i) => {
      s.classList.toggle('is-current', i === current);
      s.classList.toggle('is-done', i < current);
      s.classList.toggle('is-later', i > current);
      if (i === current) s.setAttribute('aria-current', 'step');
      else s.removeAttribute('aria-current');
    });
    // A receipt row appears once the step that produced it has happened.
    const shown = new Set(steps.slice(0, current + 1).map((s) => s.dataset.receipt).filter(Boolean));
    rows.forEach((r) => r.classList.toggle('is-pending', !shown.has(r.dataset.row)));
    spentBar.style.width = `${(spent / BUDGET) * 100}%`;
    heldBar.style.width = `${(held / BUDGET) * 100}%`;
    el('spent').textContent = usd(spent);
    el('held').textContent = usd(held);
    el('left').textContent = usd(BUDGET - spent - held);
    meter.setAttribute('aria-label', `Spent ${usd(spent)}, held ${usd(held)}, of ${usd(BUDGET)}`);
    el('pos').textContent = String(current + 1);
    // aria-disabled, not disabled: a disabled button would drop keyboard focus at either end.
    el('prev').setAttribute('aria-disabled', String(current === 0));
    el('next').setAttribute('aria-disabled', String(current === steps.length - 1));
    if (announce) {
      el('live').textContent = `Step ${current + 1} of ${steps.length}: ${step.querySelector('h3').textContent}. ` +
        `Spent ${usd(spent)} of ${usd(BUDGET)}.`;
    }
  };

  root.classList.add('lc-js');
  el('prev').closest('.lc-controls').hidden = false;
  el('prev').addEventListener('click', () => show(current - 1, true));
  el('next').addEventListener('click', () => show(current + 1, true));
  el('replay').addEventListener('click', () => { show(0, true); el('next').focus(); });
  root.addEventListener('keydown', (ev) => {
    if (ev.target.closest('a, input, textarea')) return;
    if (ev.key === 'ArrowRight') { ev.preventDefault(); show(current + 1, true); }
    if (ev.key === 'ArrowLeft') { ev.preventDefault(); show(current - 1, true); }
  });
  // Clicking a step jumps to it (pointer convenience; the buttons are the keyboard path).
  steps.forEach((s, i) => s.addEventListener('click', () => show(i, true)));
  show(0, false);
})();
