// The fixed WebGL scene: four role nodes riding a glowing loop around a core,
// over a green grid. The camera and colours glide to a pose for each page
// section, and the hero demo lights up the node for the step it is on.
import * as THREE from 'three';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';

const POSES = {
  top:       { cam: [0, 0.6, 9],     look: [0, 0, 0],      obj: [3.3, 0.2, -0.6], a: '#2bd576', b: '#0b6b3a' },
  how:       { cam: [-1.6, 2.4, 8.2], look: [0, -0.3, 0],  obj: [-2.9, 0.1, -1], a: '#38d9c4', b: '#0e5d63' },
  dashboard: { cam: [0, 1.4, 10.5],  look: [0, 0.6, -3],   obj: [0, 3.4, -10],   a: '#7dd3fc', b: '#1e3a8a' },
  runs:      { cam: [0, 3.8, 11],    look: [0, 0, -4],     obj: [0, 0.6, -7],    a: '#fbbf24', b: '#8a4b0b' },
  features:  { cam: [1.2, 0.3, 8.4], look: [0, 0, 0],      obj: [3, -0.2, -1],   a: '#a3e635', b: '#3f6212' },
  source:    { cam: [0, 0.9, 7.4],   look: [0, 0.2, 0],    obj: [0, 0.4, -1.5],  a: '#2bd576', b: '#0b6b3a' },
};
const FAIL = new THREE.Color('#f87171');
const RADIUS = 2.3;

const ringVertex = /* glsl */`
  varying vec2 vUv;
  void main() {
    vUv = uv;
    gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
  }`;
const ringFragment = /* glsl */`
  uniform vec3 uColA, uColB;
  uniform float uTime, uSpeed;
  varying vec2 vUv;
  void main() {
    float band = pow(fract(vUv.x * 4.0 - uTime * uSpeed), 8.0);
    vec3 col = mix(uColB, uColA, 0.35 + band * 0.65);
    gl_FragColor = vec4(col * (0.55 + band * 1.4), 1.0);
  }`;
const nodeVertex = /* glsl */`
  uniform float uTime, uGlow;
  varying float vShade;
  void main() {
    vec3 p = position * (1.0 + uGlow * 0.35 + 0.04 * sin(uTime * 3.0 + position.y * 6.0));
    vShade = 0.6 + 0.4 * normalize(position).y;
    gl_Position = projectionMatrix * modelViewMatrix * vec4(p, 1.0);
  }`;
const nodeFragment = /* glsl */`
  uniform vec3 uColA, uColB;
  uniform float uGlow;
  varying float vShade;
  void main() {
    vec3 col = mix(uColB, uColA, 0.4 + uGlow * 0.6);
    gl_FragColor = vec4(col * vShade * (0.55 + uGlow * 1.6), 1.0);
  }`;
const coreVertex = /* glsl */`
  varying vec3 vNormal, vView;
  void main() {
    vec4 mv = modelViewMatrix * vec4(position, 1.0);
    vNormal = normalize(normalMatrix * normal);
    vView = normalize(-mv.xyz);
    gl_Position = projectionMatrix * mv;
  }`;
const coreFragment = /* glsl */`
  uniform vec3 uColA, uColB;
  uniform float uPulse;
  varying vec3 vNormal, vView;
  void main() {
    float rim = pow(1.0 - abs(dot(vNormal, vView)), 2.2);
    vec3 col = mix(uColB * 0.25, uColA, rim);
    gl_FragColor = vec4(col * (0.45 + rim * 0.9 + uPulse * 0.8), 1.0);
  }`;
const packetVertex = /* glsl */`
  uniform float uTime, uSize, uSpeed, uRadius;
  attribute float aSeed;
  varying float vSeed;
  void main() {
    float a = aSeed * 6.2831 + uTime * uSpeed * (0.6 + aSeed * 0.5);
    float r = uRadius + sin(aSeed * 91.0) * 0.08;
    vec3 p = vec3(cos(a) * r, sin(aSeed * 53.0) * 0.06, sin(a) * r);
    vSeed = aSeed;
    vec4 mv = modelViewMatrix * vec4(p, 1.0);
    gl_PointSize = uSize * (0.5 + aSeed) / -mv.z;
    gl_Position = projectionMatrix * mv;
  }`;
const packetFragment = /* glsl */`
  uniform vec3 uColA;
  varying float vSeed;
  void main() {
    float d = length(gl_PointCoord - 0.5);
    float a = smoothstep(0.5, 0.0, d);
    gl_FragColor = vec4(uColA * a * (0.5 + vSeed * 0.7), 1.0);
  }`;
const floorVertex = /* glsl */`
  varying vec3 vWorld;
  void main() {
    vec4 w = modelMatrix * vec4(position, 1.0);
    vWorld = w.xyz;
    gl_Position = projectionMatrix * viewMatrix * w;
  }`;
const floorFragment = /* glsl */`
  uniform float uTime;
  uniform vec3 uCol, uCam;
  varying vec3 vWorld;
  void main() {
    vec2 p = vWorld.xz * 0.5;
    p.y += uTime * 0.35;
    vec2 g = abs(fract(p - 0.5) - 0.5) / fwidth(p);
    float line = 1.0 - min(min(g.x, g.y), 1.0);
    float fade = smoothstep(40.0, 5.0, length(vWorld.xz - uCam.xz));
    gl_FragColor = vec4(uCol * line * fade * 0.16, 1.0);
  }`;
const dustVertex = /* glsl */`
  uniform float uTime, uSize;
  attribute float aSeed;
  void main() {
    vec3 p = position;
    p.y = mod(p.y + uTime * (0.2 + aSeed * 0.4), 9.0) - 3.5;
    p.x += sin(uTime * 0.6 + aSeed * 40.0) * 0.25;
    vec4 mv = modelViewMatrix * vec4(p, 1.0);
    gl_PointSize = uSize * (0.6 + aSeed) / -mv.z;
    gl_Position = projectionMatrix * mv;
  }`;
const dustFragment = /* glsl */`
  uniform vec3 uCol;
  void main() {
    float d = length(gl_PointCoord - 0.5);
    gl_FragColor = vec4(uCol * smoothstep(0.5, 0.0, d) * 0.18, 1.0);
  }`;

function glow(extra) {
  return { transparent: true, depthWrite: false, blending: THREE.AdditiveBlending, ...extra };
}

function seededPoints(count, place) {
  const seeds = new Float32Array(count);
  const spots = new Float32Array(count * 3);
  for (let i = 0; i < count; i++) {
    seeds[i] = Math.random();
    spots.set(place(), i * 3);
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute('position', new THREE.BufferAttribute(spots, 3));
  geometry.setAttribute('aSeed', new THREE.BufferAttribute(seeds, 1));
  return geometry;
}

export function createWorld(canvas, { reducedMotion }) {
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, powerPreference: 'high-performance' });
  renderer.setClearColor(0x040806, 1);
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(42, 1, 0.1, 200);

  const colA = new THREE.Color(POSES.top.a);
  const colB = new THREE.Color(POSES.top.b);
  const time = { value: 0 };
  const speed = { value: 0.12 };

  // The loop, tilted towards the viewer, with the core at its centre.
  const sculpture = new THREE.Group();
  scene.add(sculpture);
  const loop = new THREE.Group();
  loop.rotation.x = 1.1;
  sculpture.add(loop);

  const ringShape = new THREE.TorusGeometry(RADIUS, 0.022, 8, 220);
  const ring = new THREE.Mesh(ringShape, new THREE.ShaderMaterial(glow({
    uniforms: { uColA: { value: colA }, uColB: { value: colB }, uTime: time, uSpeed: speed },
    vertexShader: ringVertex, fragmentShader: ringFragment,
  })));
  ring.rotation.x = Math.PI / 2;
  loop.add(ring);

  const packets = new THREE.Points(seededPoints(110, () => [0, 0, 0]), new THREE.ShaderMaterial(glow({
    uniforms: { uColA: { value: colA }, uTime: time, uSpeed: speed, uSize: { value: 40 }, uRadius: { value: RADIUS } },
    vertexShader: packetVertex, fragmentShader: packetFragment,
  })));
  loop.add(packets);

  const nodeShape = new THREE.WireframeGeometry(new THREE.IcosahedronGeometry(0.3, 1));
  const nodes = [0, 1, 2, 3].map((i) => {
    const uniforms = { uColA: { value: colA }, uColB: { value: colB }, uTime: time, uGlow: { value: 0 } };
    const node = new THREE.LineSegments(nodeShape, new THREE.ShaderMaterial(glow({
      uniforms, vertexShader: nodeVertex, fragmentShader: nodeFragment,
    })));
    const angle = Math.PI / 2 + i * (Math.PI / 2);
    node.position.set(Math.cos(angle) * RADIUS, 0, Math.sin(angle) * RADIUS);
    loop.add(node);
    return { node, uniforms };
  });

  const coreUniforms = { uColA: { value: colA }, uColB: { value: colB }, uPulse: { value: 0 } };
  const core = new THREE.Mesh(new THREE.IcosahedronGeometry(0.85, 4), new THREE.ShaderMaterial(glow({
    uniforms: coreUniforms, vertexShader: coreVertex, fragmentShader: coreFragment,
  })));
  sculpture.add(core);
  const shell = new THREE.LineSegments(new THREE.WireframeGeometry(new THREE.IcosahedronGeometry(1.05, 1)),
    new THREE.LineBasicMaterial(glow({ color: colB.clone(), opacity: 0.6 })));
  sculpture.add(shell);

  const floorUniforms = { uTime: time, uCol: { value: colB.clone() }, uCam: { value: camera.position } };
  const floor = new THREE.Mesh(new THREE.PlaneGeometry(160, 160), new THREE.ShaderMaterial(glow({
    uniforms: floorUniforms, vertexShader: floorVertex, fragmentShader: floorFragment,
  })));
  floor.rotation.x = -Math.PI / 2;
  floor.position.y = -2.6;
  scene.add(floor);

  const dustUniforms = { uTime: time, uSize: { value: 40 }, uCol: { value: colA } };
  scene.add(new THREE.Points(seededPoints(420, () => {
    const r = 2 + Math.random() * 9;
    const a = Math.random() * Math.PI * 2;
    return [Math.cos(a) * r, Math.random() * 9, Math.sin(a) * r - 2];
  }), new THREE.ShaderMaterial(glow({ uniforms: dustUniforms, vertexShader: dustVertex, fragmentShader: dustFragment }))));

  const composer = new EffectComposer(renderer);
  composer.addPass(new RenderPass(scene, camera));
  const bloom = new UnrealBloomPass(new THREE.Vector2(1, 1), 0.8, 0.45, 0.05);
  composer.addPass(bloom);
  composer.addPass(new OutputPass());

  // Section poses, blended by scroll position.
  const sections = [...document.querySelectorAll('main > section')]
    .map((el) => ({ el, pose: POSES[el.id] })).filter((s) => s.pose);
  const vec = (v) => new THREE.Vector3(...v);
  const state = { cam: vec(POSES.top.cam), look: vec(POSES.top.look), obj: vec(POSES.top.obj), a: colA.clone(), b: colB.clone() };
  const target = { cam: new THREE.Vector3(), look: new THREE.Vector3(), obj: new THREE.Vector3(), a: new THREE.Color(), b: new THREE.Color() };
  const pointer = { x: 0, y: 0 };
  const demo = { step: -1, failed: 0, passed: 0 };
  let narrow = false, spin = 0, last = performance.now(), first = true;

  function resize() {
    const w = window.innerWidth, h = window.innerHeight;
    narrow = w / h < 0.9 || w < 960;
    const ratio = Math.min(window.devicePixelRatio || 1, narrow ? 1.25 : 1.6);
    renderer.setPixelRatio(ratio);
    renderer.setSize(w, h, false);
    composer.setPixelRatio(ratio);
    composer.setSize(w, h);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  }

  function poseTarget() {
    const center = window.scrollY + window.innerHeight * 0.5;
    let i = 0;
    while (i < sections.length - 1 && sections[i + 1].el.offsetTop <= center) i++;
    const from = sections[i], to = sections[Math.min(i + 1, sections.length - 1)];
    const span = Math.max(1, to.el.offsetTop - from.el.offsetTop);
    let t = from === to ? 0 : (center - from.el.offsetTop) / span;
    t = THREE.MathUtils.smoothstep(t, 0.3, 0.95);
    for (const key of ['cam', 'look', 'obj']) {
      target[key].fromArray(from.pose[key]).lerp(vec(to.pose[key]), t);
    }
    target.a.set(from.pose.a).lerp(new THREE.Color(to.pose.a), t);
    target.b.set(from.pose.b).lerp(new THREE.Color(to.pose.b), t);
    if (demo.failed > 0) target.a.lerp(FAIL, demo.failed);
    if (narrow) {
      // On phones and tall screens the copy sits over the scene, so centre it up top.
      target.obj.x = 0;
      target.obj.y += 2.3;
      target.look.y += 1.1;
    }
  }

  function frame(now) {
    const dt = Math.min(0.05, (now - last) / 1000);
    last = now;
    const motion = reducedMotion ? 0.15 : 1;
    time.value += dt * motion;
    demo.failed = Math.max(0, demo.failed - dt * 0.7);
    demo.passed = Math.max(0, demo.passed - dt * 0.5);

    poseTarget();
    // Start at the pose for wherever the page opened, then glide from there.
    const ease = first ? 1 : 1 - Math.exp(-dt * 3.2);
    const colourEase = first ? 1 : 1 - Math.exp(-dt * 6);
    first = false;
    state.cam.lerp(target.cam, ease);
    state.look.lerp(target.look, ease);
    state.obj.lerp(target.obj, ease);
    state.a.lerp(target.a, colourEase);
    state.b.lerp(target.b, ease);

    const busy = demo.step >= 0;
    speed.value += ((busy ? 0.45 : 0.12) - speed.value) * Math.min(1, dt * 2);
    spin += dt * motion * (busy ? 0.5 : 0.18);

    camera.position.copy(state.cam).add(new THREE.Vector3(pointer.x * 0.5, -pointer.y * 0.3, 0));
    camera.lookAt(state.look);
    sculpture.position.copy(state.obj);
    sculpture.scale.setScalar(narrow ? 0.55 : 0.85);
    loop.rotation.y = spin;
    loop.rotation.z = Math.sin(time.value * 0.3) * 0.08 + pointer.x * 0.15;
    shell.rotation.y = -spin * 0.6;
    shell.rotation.x = time.value * 0.1;
    core.scale.setScalar(1 + demo.passed * 0.25);

    nodes.forEach(({ node, uniforms }, i) => {
      const on = demo.step === i ? 1 : 0;
      uniforms.uGlow.value += (on - uniforms.uGlow.value) * Math.min(1, dt * 5);
      node.rotation.y = time.value * (0.6 + i * 0.1);
      node.rotation.x = time.value * 0.4;
    });

    colA.copy(state.a);
    colB.copy(state.b);
    floorUniforms.uCol.value.copy(state.b).lerp(state.a, 0.35);
    shell.material.color.copy(state.b).lerp(state.a, 0.3);
    coreUniforms.uPulse.value = demo.passed;
    dustUniforms.uSize.value = 34 * renderer.getPixelRatio();
    packets.material.uniforms.uSize.value = 46 * renderer.getPixelRatio();
    bloom.strength = 0.75 + demo.passed * 0.6 + (busy ? 0.15 : 0);

    composer.render(dt);
    requestAnimationFrame(frame);
  }

  window.addEventListener('resize', resize);
  window.addEventListener('pointermove', (e) => {
    pointer.x = e.clientX / window.innerWidth - 0.5;
    pointer.y = e.clientY / window.innerHeight - 0.5;
  }, { passive: true });
  resize();
  requestAnimationFrame(frame);

  // The hero demo drives these: which role is working, and how a step ended.
  return {
    step(index) { demo.step = index; },
    fail() { demo.failed = 1; },
    pass() { demo.passed = 1; },
  };
}
