// A stand-in for a Three.js game: plain objects shaped as Three.js keeps them (name, type, children, matrixWorld,
// geometry.boundingBox), a camera, a player that rolls forward while W is down, and a DOM-less key target. The bridge's
// tests (bridge.test.mjs) and the Python tests (tests/test_threejs.py, through `node fake_game.mjs <url>`) drive it.
// No three, no browser.

import { createBridge } from "../autopilot-bridge.js";

function translation(x, y, z) {
  return { elements: [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, x, y, z, 1] };
}

export function object(name, type, at = [0, 0, 0], half = null, userData = {}) {
  const o = { name, type, children: [], parent: null, visible: true, userData, matrixWorld: translation(...at), isObject3D: true };
  if (half) o.geometry = { boundingBox: { min: { x: -half[0], y: -half[1], z: -half[2] }, max: { x: half[0], y: half[1], z: half[2] } } };
  if (type === "Mesh") o.isMesh = true;
  return o;
}

export function add(parent, child) {
  child.parent = parent;
  parent.children.push(child);
  // a child's matrixWorld is its parent's translation plus its own (no rotations in this stand-in)
  const p = parent.matrixWorld.elements;
  const c = child.matrixWorld.elements;
  c[12] += p[12];
  c[13] += p[13];
  c[14] += p[14];
  return child;
}

// A camera at `at` looking down -z: fov 60, aspect 1, near 0.1, far 100.
export function camera(at = [0, 2, 10]) {
  const f = 1 / Math.tan(Math.PI / 6);
  const near = 0.1;
  const far = 100;
  const cam = object("Camera", "PerspectiveCamera", at);
  cam.isCamera = true;
  cam.fov = 60;
  cam.aspect = 1;
  cam.matrixWorldInverse = translation(-at[0], -at[1], -at[2]);
  cam.projectionMatrix = { elements: [f, 0, 0, 0, 0, f, 0, 0, 0, 0, (far + near) / (near - far), -1, 0, 0, (2 * far * near) / (near - far), 0] };
  return cam;
}

export function makeFakeGame({ url, WebSocket } = {}) {
  const scene = object("Scene", "Scene");
  const ground = add(scene, object("Ground", "Mesh", [0, -0.05, 0], [100, 0.05, 100]));
  const rail = add(scene, object("Rail", "Mesh", [2, 0.5, 0], [0.05, 0.05, 3], { class: "Rail", thing: true }));
  const rail2 = add(scene, object("Rail", "Mesh", [-2, 0.5, 0], [0.05, 0.05, 3], { class: "Rail" }));
  const props = add(scene, object("", "Group"));
  const cone = add(props, object("Cone", "Mesh", [0, 0.4, -4], [0.3, 0.4, 0.3], { thing: true, label: "cone" }));
  const player = add(scene, object("Player", "Group", [0, 0, 5]));
  add(player, object("Body", "Mesh", [0, 0.9, 0], [0.3, 0.9, 0.3]));
  const cam = add(scene, camera([0, 2, 10]));
  const down = new Set();
  const keyLog = [];
  const keyTarget = {
    dispatchEvent(ev) {
      keyLog.push(`${ev.type}:${ev.code}`);
      if (ev.type === "keydown") down.add(ev.code);
      if (ev.type === "keyup") down.delete(ev.code);
      return true;
    },
  };
  const state = { position: [0, 0, 5], speed: 0, restarts: 0, paused: false, time: 0, score: 7 };

  // One fixed step: W pushes the player toward -z (the camera's forward) at 4 units a second.
  function step(dt) {
    if (state.paused) return;
    state.time += dt;
    state.speed = down.has("KeyW") ? 4 : 0;
    state.position[2] -= state.speed * dt;
    player.matrixWorld.elements[14] = state.position[2];
    player.children[0].matrixWorld.elements[14] = state.position[2];
  }

  let frameFns = [];
  const requestFrame = (fn) => frameFns.push(fn);
  let clock = 0;
  const pump = (n = 1, ms = 16) => {
    for (let i = 0; i < n; i += 1) {
      clock += ms;
      step(ms / 1000);
      const fns = frameFns;
      frameFns = [];
      for (const fn of fns) fn(clock);
    }
  };

  const hooks = {
    player: () => ({ object: player, position: [...state.position], velocity: [0, 0, -state.speed], forward: [0, 0, -1], onGround: true, halfHeight: 0.9 }),
    movement: () => ({ walkSpeed: 4, jumpSpeed: 5, gravity: -9.8, halfHeight: 0.9, radius: 0.3, maxSlope: 40 }),
    controls: () => [{ name: "push", keys: ["KeyW", "ArrowUp"] }, { name: "jump", keys: ["Space"] }],
    restart: () => {
      state.position = [0, 0, 5];
      state.restarts += 1;
      step(0);
    },
    pause: (p) => (state.paused = p),
    paused: () => state.paused,
    gameTime: () => state.time,
    objects: { state, game: { addPoints: (n) => (state.score += Number(n.points ?? n)) } },
  };

  const nav = { getGamepads: () => [] };
  const bridge = createBridge({
    url, WebSocket, scene, camera: cam, canvas: null, keyTarget, hooks, project: "Fake Rink",
    global: { navigator: nav, addEventListener() {}, removeEventListener() {} }, document: null,
    now: () => clock, requestFrame,
  });
  return { bridge, scene, player, rail, rail2, cone, ground, camera: cam, state, down, keyLog, pump, nav };
}

// `node fake_game.mjs ws://127.0.0.1:<port>/v1/bridge`: connect to that agent service and run frames in real time
// until it closes the connection.
if (process.argv[1] && import.meta.url.endsWith(process.argv[1].split("/").pop()) && process.argv[2]) {
  const game = makeFakeGame({ url: process.argv[2], WebSocket: globalThis.WebSocket });
  game.bridge.connect();
  const timer = setInterval(() => {
    game.pump(1, 16);
    if (game.bridge.state === "connecting" && game.bridge.session) {
      clearInterval(timer);
      game.bridge.close();
      process.exit(0);
    }
  }, 16);
  setTimeout(() => process.exit(3), 120000);
}
