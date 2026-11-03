// Delegatron site: hand-drawn marks (Rough.js), the pinned run replay (GSAP
// ScrollTrigger) and the dashboard tabs. Everything here is enhancement: with
// no JavaScript the page shows every row, note and screen as plain content.
const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;
const darkScheme = matchMedia('(prefers-color-scheme: dark)');
const hasRough = typeof window.rough !== 'undefined';
if (hasRough) document.documentElement.classList.add('js');

// ---------- Hand-drawn marks ----------
// Each mark is an SVG laid over its target and redrawn on resize, with a fixed
// seed so the stroke keeps its shape from one draw to the next.
const pen = () => getComputedStyle(document.documentElement).getPropertyValue('--pen').trim();
const marks = [];

function svgFor(host, x, y, w, h) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', 'rough');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('width', w);
  svg.setAttribute('height', h);
  svg.style.left = `${x}px`;
  svg.style.top = `${y}px`;
  host.appendChild(svg);
  return svg;
}

// shape(rc, w, h) returns the Rough node; box(host) returns where to draw it.
function addMark(host, box, shape, opts = {}) {
  const mark = { host, box, shape, svg: null, drawn: false, ...opts };
  marks.push(mark);
  return mark;
}

function render(mark, animate) {
  mark.svg?.remove();
  const { x, y, w, h } = mark.box(mark.host);
  const svg = svgFor(mark.host, x, y, w, h);
  const rc = window.rough.svg(svg);
  svg.appendChild(mark.shape(rc, w, h, { stroke: pen(), strokeWidth: mark.width ?? 2, roughness: 1.6, bowing: 1.2, seed: mark.seed ?? 7 }));
  mark.svg = svg;
  if (!animate || reducedMotion) return;
  let delay = 0;
  for (const path of svg.querySelectorAll('path')) {
    const length = path.getTotalLength();
    const duration = Math.min(900, 220 + length * 1.4);
    path.style.strokeDasharray = `${length}`;
    path.style.strokeDashoffset = `${length}`;
    path.animate([{ strokeDashoffset: length }, { strokeDashoffset: 0 }],
      { duration, delay, easing: 'cubic-bezier(.6,.05,.3,1)', fill: 'forwards' });
    delay += duration * 0.7;
  }
}

function draw(mark) {
  if (mark.drawn) return;
  mark.drawn = true;
  render(mark, true);
}

const PAD = 10;
const around = (el) => ({ x: -PAD - 4, y: -PAD, w: el.offsetWidth + PAD * 2 + 8, h: el.offsetHeight + PAD * 2 });
// An underline may wrap across lines, so it is drawn over its block parent,
// one stroke under each line box of the inline element.
function lineStrokes(span) {
  const host = span.parentElement;
  return {
    host,
    box: () => ({ x: 0, y: 0, w: host.offsetWidth, h: host.offsetHeight + 16 }),
    shape: (rc, w, h, o) => {
      const g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
      const origin = host.getBoundingClientRect();
      [...span.getClientRects()].forEach((r, i) => {
        const x0 = r.left - origin.left - 2;
        const x1 = r.right - origin.left + 2;
        const y = r.bottom - origin.top - 4;
        g.appendChild(rc.curve([[x0, y + 2], [x0 + (x1 - x0) * 0.4, y - 2], [x0 + (x1 - x0) * 0.75, y + 3], [x1, y - 1]], { ...o, seed: o.seed + i }));
      });
      return g;
    },
  };
}

if (hasRough) {
  // Inline marks named in the markup: underline or circle.
  for (const el of document.querySelectorAll('[data-rough]')) {
    let mark;
    if (el.dataset.rough === 'underline') {
      const { host, box, shape } = lineStrokes(el);
      mark = addMark(host, box, shape, { width: 2.6 });
    } else {
      mark = addMark(el, around, (rc, w, h, o) => rc.ellipse(w / 2, h / 2, w - 4, h - 4, o), { width: 1.8 });
    }
    mark.lazy = true;
  }

  // A bracket in the margin of the run log, around the four identical failures.
  const logWrap = document.querySelector('.log-wrap');
  const loop = [...document.querySelectorAll('.log li.loopback')];
  if (logWrap && loop.length) {
    const bracket = addMark(logWrap, () => {
      const top = loop[0].offsetTop;
      const bottom = loop.at(-1).offsetTop + loop.at(-1).offsetHeight;
      const narrow = window.innerWidth < 600;
      return { x: narrow ? -15 : -26, y: top + 4, w: narrow ? 13 : 22, h: bottom - top - 8 };
    }, (rc, w, h, o) => rc.curve([[w - 2, 2], [6, 10], [4, h / 2], [6, h - 10], [w - 2, h - 2]], o), { width: 2, seed: 3 });
    bracket.group = 'replay';
  }

  // The stall stamp's box, inked when the replay reaches the end.
  const stamp = document.getElementById('stamp');
  if (stamp) {
    const box = addMark(stamp, (el) => ({ x: -6, y: -6, w: el.offsetWidth + 12, h: el.offsetHeight + 12 }),
      (rc, w, h, o) => rc.rectangle(4, 4, w - 8, h - 8, { ...o, strokeWidth: 3, roughness: 2.2 }), { seed: 11 });
    box.group = 'replay';
  }

  const lazyObserver = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      lazyObserver.unobserve(entry.target);
      marks.filter((m) => m.host === entry.target).forEach(draw);
    }
  }, { rootMargin: '0px 0px -20% 0px' });
  for (const mark of marks.filter((m) => m.lazy)) lazyObserver.observe(mark.host);

  let resizeTimer;
  const redrawAll = () => marks.filter((m) => m.drawn).forEach((m) => render(m, false));
  window.addEventListener('resize', () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(redrawAll, 150); });
  darkScheme.addEventListener('change', redrawAll);
  document.fonts?.ready.then(redrawAll);
}

const replayMarks = () => marks.filter((m) => m.group === 'replay');

// ---------- Run replay ----------
// Wide screens with motion: the run is pinned and each scroll step adds one
// iteration. Otherwise every row is already on the page; we only ink the marks.
const replay = document.getElementById('replay');
const rows = [...document.querySelectorAll('#log li')];
const status = document.getElementById('replay-status');
const stampEl = document.getElementById('stamp');
const finalStatus = status.textContent;

function revealReplayStatic() {
  new IntersectionObserver(([entry], observer) => {
    if (!entry.isIntersecting) return;
    observer.disconnect();
    replayMarks().forEach(draw);
  }, { threshold: 0.4 }).observe(replay);
}

if (window.gsap && window.ScrollTrigger) {
  gsap.registerPlugin(ScrollTrigger);
  const mm = gsap.matchMedia();

  mm.add('(min-width: 861px) and (prefers-reduced-motion: no-preference)', () => {
    gsap.set(rows, { autoAlpha: 0, y: 10 });
    gsap.set(stampEl, { autoAlpha: 0, scale: 1.5 });
    status.textContent = 'Scroll to step through the run.';
    let shown = -1;

    const tl = gsap.timeline({
      defaults: { ease: 'power2.out' },
      scrollTrigger: {
        trigger: replay,
        start: 'top 96px',
        end: '+=1700',
        pin: true,
        scrub: 0.5,
        onUpdate(self) {
          const step = Math.min(rows.length, Math.floor(self.progress * (rows.length + 1.2)));
          if (step === shown) return;
          shown = step;
          status.textContent = step === 0 ? 'Scroll to step through the run.' : rows[step - 1].dataset.say;
          if (step === rows.length) replayMarks().forEach(draw);
        },
      },
    });
    rows.forEach((row) => tl.to(row, { autoAlpha: 1, y: 0, duration: 1 }));
    tl.to(stampEl, { autoAlpha: 1, scale: 1, duration: 0.5, ease: 'back.out(2)' }, '+=0.2');

    return () => {
      status.textContent = finalStatus;
      gsap.set([...rows, stampEl], { clearProps: 'all' });
    };
  });

  mm.add('(max-width: 860px), (prefers-reduced-motion: reduce)', revealReplayStatic);
} else {
  revealReplayStatic();
}

// ---------- Dashboard screens ----------
// Tabs that cycle on their own until one is chosen.
const panel = document.getElementById('shot');
const shot = panel.querySelector('img');
const tabs = [...document.querySelectorAll('.shots button')];
let tabTimer;
function showTab(tab) {
  for (const other of tabs) {
    other.setAttribute('aria-selected', String(other === tab));
    other.tabIndex = other === tab ? 0 : -1;
  }
  panel.setAttribute('aria-labelledby', tab.id);
  shot.classList.add('swap');
  setTimeout(() => {
    shot.src = `/media/${tab.dataset.shot}.webp`;
    shot.alt = tab.dataset.alt;
    shot.classList.remove('swap');
  }, 200);
}
function choose(tab) {
  clearInterval(tabTimer);
  showTab(tab);
  tab.focus();
}
tabs.forEach((tab, i) => {
  tab.addEventListener('click', () => choose(tab));
  tab.addEventListener('keydown', (e) => {
    const step = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
    if (step) choose(tabs[(i + step + tabs.length) % tabs.length]);
  });
});
new IntersectionObserver(([entry], observer) => {
  if (!entry.isIntersecting) return;
  observer.disconnect();
  for (const tab of tabs.slice(1)) new Image().src = `/media/${tab.dataset.shot}.webp`;
  if (!reducedMotion) {
    tabTimer = setInterval(() => {
      const next = (tabs.findIndex((t) => t.getAttribute('aria-selected') === 'true') + 1) % tabs.length;
      showTab(tabs[next]);
    }, 5000);
  }
}, { threshold: 0.25 }).observe(panel);
