const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;
let world = null;

// The 3D scene is decoration: without WebGL the page falls back to a gradient.
function hasWebGL() {
  try {
    const probe = document.createElement('canvas');
    return Boolean(probe.getContext('webgl2') || probe.getContext('webgl'));
  } catch {
    return false;
  }
}
if (hasWebGL()) {
  import('./world.js')
    .then(({ createWorld }) => { world = createWorld(document.getElementById('world'), { reducedMotion }); })
    .catch((error) => {
      console.warn('3D scene unavailable:', error);
      document.documentElement.classList.add('no-webgl');
    });
} else {
  document.documentElement.classList.add('no-webgl');
}

// Loop demo: a scripted run of the visitor's task. Their words are only ever
// written with textContent.
const form = document.getElementById('ask');
const input = document.getElementById('task');
const runButton = form.querySelector('button');
const verdict = document.getElementById('verdict');
const rows = [...document.querySelectorAll('#loop li')];
const ROLE = { plan: 0, build: 1, test: 2, review: 3 };
const wait = (ms) => new Promise((resolve) => { setTimeout(resolve, reducedMotion ? ms * 0.5 : ms); });

function show(step, text, mark) {
  const row = rows[ROLE[step]];
  row.querySelector('span').textContent = text;
  row.classList.remove('active', 'done', 'failed');
  if (mark) row.classList.add(mark);
  world?.step(mark === 'active' ? ROLE[step] : -1);
}

async function run(task) {
  const tests = 5 + (task.length % 5);
  const broken = 1 + (task.length % 2);
  for (const row of rows) {
    row.classList.remove('active', 'done', 'failed');
    row.querySelector('span').textContent = 'Waiting';
  }
  verdict.classList.remove('ok');
  verdict.textContent = 'Iteration 1 of 5';

  show('plan', `Planning “${task}”`, 'active');
  await wait(1300);
  show('plan', `3 tasks, ${tests} acceptance tests`, 'done');
  show('build', 'Writing the code and its tests', 'active');
  await wait(1400);
  show('build', `2 files, ${tests} tests`, 'done');
  show('test', `Running ${tests} tests in the sandbox`, 'active');
  await wait(1200);
  show('test', `${tests - broken} passed, ${broken} failed`, 'failed');
  world?.fail();
  await wait(1100);

  verdict.textContent = 'Iteration 2 of 5 · going around with the failures in hand';
  show('build', `Fixing ${broken} failing ${broken === 1 ? 'test' : 'tests'}`, 'active');
  await wait(1300);
  show('build', '1 file changed', 'done');
  show('test', `Running ${tests} tests in the sandbox`, 'active');
  await wait(1100);
  show('test', `${tests} of ${tests} passed`, 'done');
  show('review', 'Reading the change and the evidence', 'active');
  await wait(1400);
  show('review', 'Approved', 'done');
  world?.pass();

  verdict.classList.add('ok');
  verdict.textContent = `Done in 2 iterations: “${task}” is tested and reviewed. (Simulated run.)`;
}

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const task = input.value.trim().replace(/\s+/g, ' ') || input.defaultValue;
  runButton.disabled = true;
  try {
    await run(task);
  } finally {
    runButton.disabled = false;
  }
});

// Header backdrop, side dots and the section accent colour.
const header = document.querySelector('.top');
const onScroll = () => header.classList.toggle('scrolled', window.scrollY > 40);
window.addEventListener('scroll', onScroll, { passive: true });
onScroll();

const dots = new Map([...document.querySelectorAll('.dots a')].map((a) => [a.hash.slice(1), a]));
const sectionObserver = new IntersectionObserver((entries) => {
  for (const entry of entries) {
    if (!entry.isIntersecting) continue;
    document.documentElement.style.setProperty('--accent', entry.target.dataset.accent);
    for (const [id, dot] of dots) dot.classList.toggle('active', id === entry.target.id);
  }
}, { rootMargin: '-45% 0px -45% 0px' });
document.querySelectorAll('main > section').forEach((s) => sectionObserver.observe(s));

// Dashboard screenshots: tabs that cycle on their own until one is chosen.
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
  entry.target.classList.add('in');
  observer.disconnect();
  for (const tab of tabs.slice(1)) new Image().src = `/media/${tab.dataset.shot}.webp`;
  if (!reducedMotion) {
    tabTimer = setInterval(() => {
      const next = (tabs.findIndex((t) => t.getAttribute('aria-selected') === 'true') + 1) % tabs.length;
      showTab(tabs[next]);
    }, 4500);
  }
}, { threshold: 0.25 }).observe(panel);
