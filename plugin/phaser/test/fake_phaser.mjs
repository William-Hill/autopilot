// A stand-in for a Phaser game: plain objects shaped as Phaser keeps them (a game with a scene manager, a loop, events
// and a WebGL renderer; scenes with a display list and a main camera; game objects with x, y, rotation, scale, size,
// origin, scroll factor, parentContainer and list), a side-view player that runs right while D is down, and a
// DOM-less key target. The helper's tests (phaser.test.mjs) and the Python tests (tests/test_phaser.py, through
// `node fake_phaser.mjs <url>`) drive it. No phaser, no browser.

import { createBridge } from "../../threejs/autopilot-bridge.js";
import { phaserBridgeOptions } from "../autopilot-phaser.js";
import { realpathSync } from "node:fs";
import { pathToFileURL } from "node:url";

function emitter() {
  const on = new Map();
  return {
    on(name, fn) {
      if (!on.has(name)) on.set(name, []);
      on.get(name).push(fn);
    },
    off(name, fn) {
      on.set(name, (on.get(name) ?? []).filter((f) => f !== fn));
    },
    emit(name, ...args) {
      for (const fn of [...(on.get(name) ?? [])]) fn(...args);
    },
  };
}

export function gameObject(type, props = {}) {
  const data = { ...(props.data ?? {}) };
  delete props.data;
  return {
    type, name: "", x: 0, y: 0, rotation: 0, scaleX: 1, scaleY: 1, width: 0, height: 0, originX: 0.5, originY: 0.5,
    scrollFactorX: 1, scrollFactorY: 1, visible: true, active: true, parentContainer: null, flipX: false,
    getData: (k) => data[k], setData: (k, v) => (data[k] = v),
    ...props,
  };
}

export function container(props, children) {
  const c = gameObject("Container", { ...props, list: [] });
  for (const k of children) {
    k.parentContainer = c;
    c.list.push(k);
  }
  return c;
}

export function camera(x, y, width, height, zoom = 1) {
  return { name: "", zoom, scrollX: x, scrollY: y, worldView: { x, y, width, height } };
}

// A fake canvas: what the mirror and the bridge's picture() call on it.
export function fakeCanvas(width = 0, height = 0) {
  const c = {
    width, height, drawn: 0,
    getContext: () => ({ drawImage: () => (c.drawn += 1) }),
    toDataURL: () => "data:image/jpeg;base64,AAAA",
    getBoundingClientRect: () => ({ left: 0, top: 0, width: c.width, height: c.height }),
    dispatchEvent: () => true,
  };
  return c;
}

export function makeFakePhaser({ url, WebSocket, options = {} } = {}) {
  const events = emitter();
  const ground = gameObject("Rectangle", { name: "ground", x: 480, y: 520, width: 4000, height: 40 });
  const player = gameObject("Sprite", { name: "player", x: 100, y: 400, width: 32, height: 64, originX: 0.5, originY: 1 });
  player.body = { velocity: { x: 0, y: 0 }, blocked: { down: true }, touching: { down: false } };
  const coin1 = gameObject("Sprite", { name: "coin", x: 0, y: 0, width: 16, height: 16, data: { class: "Coin" } });
  const coin2 = gameObject("Sprite", { name: "coin", x: 100, y: 0, width: 16, height: 16, data: { class: "Coin" } });
  const coins = container({ name: "coins", x: 300, y: 300 }, [coin1, coin2]);
  const crate = gameObject("Image", { name: "crate", x: 600, y: 480, width: 40, height: 40, rotation: Math.PI / 2 });
  const lives = gameObject("Text", { name: "lives", x: 900, y: 20, width: 40, height: 20, text: "Lives 3", scrollFactorX: 0, scrollFactorY: 0 });
  const score = gameObject("Text", { name: "score", x: 20, y: 20, width: 80, height: 20, text: "Score 0" });

  const level = {
    sys: { settings: { key: "Level", visible: true } },
    children: { list: [ground, player, coins, crate, lives] },
    cameras: { main: camera(0, 0, 960, 540) },
    time: { now: 0 },
    restarts: 0,
    started: null,
  };
  level.scene = {
    key: "Level",
    restart: () => {
      level.restarts += 1;
      player.x = 100;
      player.y = 400;
    },
    start: (key) => (level.started = key),
  };
  const hud = { sys: { settings: { key: "HUD", visible: true } }, children: { list: [score] }, cameras: { main: camera(0, 0, 960, 540) }, scene: { key: "HUD" } };
  const boot = { sys: { settings: { key: "Boot" } }, children: { list: [] } };
  const active = [level, hud];
  const canvas = fakeCanvas(960, 540);

  const game = {
    canvas, events, isPaused: false,
    config: { renderType: 2, preserveDrawingBuffer: false },
    renderer: { type: 2, gl: {} },
    loop: { frame: 0, time: 0 },
    scene: { scenes: [boot, level, hud], getScenes: (isActive = true) => (isActive ? active : [boot, level, hud]), getScene: (k) => [boot, level, hud].find((s) => s.sys.settings.key === k) },
    pause() {
      this.isPaused = true;
    },
    resume() {
      this.isPaused = false;
    },
  };

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
  const state = { score: 0, target: coin2 };

  // One frame: D runs the player right at 200 pixels a second (A left), and the camera follows it.
  function step(dt) {
    if (game.isPaused) return;
    const vx = down.has("KeyD") ? 200 : down.has("KeyA") ? -200 : 0;
    player.body.velocity.x = vx;
    if (vx) player.flipX = vx < 0;
    player.x += vx * dt;
    level.cameras.main.worldView.x = Math.max(0, player.x - 480);
    level.time.now += dt * 1000;
    game.loop.time += dt * 1000;
    game.loop.frame += 1;
    events.emit("step", game.loop.time, dt * 1000);
    events.emit("postrender");
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

  const document = { createElement: () => fakeCanvas() };
  const nav = { getGamepads: () => [] };
  const opts = phaserBridgeOptions(game, {
    url, WebSocket, project: "Fake Runner", phaser: { VERSION: "4.2.0" }, keyTarget, document,
    global: { navigator: nav, addEventListener() {}, removeEventListener() {} },
    now: () => clock, requestFrame,
    hooks: {
      player: () => ({ object: player, state: "running" }),
      movement: () => ({ walkSpeed: 200, jumpSpeed: 500, gravity: 1200 }),
      controls: () => [{ name: "right", keys: ["KeyD", "ArrowRight"] }, { name: "jump", keys: ["Space"] }],
      objects: { state, game: { addPoints: (n) => (state.score += Number(n.points ?? n)) } },
    },
    ...options,
  });
  const bridge = createBridge(opts);
  return { bridge, game, level, hud, player, coins, coin1, coin2, crate, lives, score, ground, state, down, keyLog, pump, canvas };
}

// `node fake_phaser.mjs ws://127.0.0.1:<port>/v1/bridge`: connect to that agent service and run frames in real time
// until it closes the connection.
// (as a real path's file URL: on Windows argv[1] has backslashes, which a "/" split leaves whole)
if (process.argv[1] && import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href && process.argv[2]) {
  const fake = makeFakePhaser({ url: process.argv[2], WebSocket: globalThis.WebSocket });
  fake.bridge.connect();
  const timer = setInterval(() => {
    fake.pump(1, 16);
    if (fake.bridge.state === "connecting" && fake.bridge.session) {
      clearInterval(timer);
      fake.bridge.close();
      process.exit(0);
    }
  }, 16);
  setTimeout(() => process.exit(3), 120000);
}
