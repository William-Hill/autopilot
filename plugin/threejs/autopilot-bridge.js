// Autopilot's bridge for Three.js games in the browser (docs: plugin/threejs/README.md).
//
// A game imports this module and calls connectBridge(). The bridge connects out to the agent service over a WebSocket
// (ws://127.0.0.1:8765/v1/bridge), says hello as engine "threejs", and answers the engine-neutral ops that the Unity
// and Godot plugins answer: find, transforms, pose, movement, view, watch, key, input, move, pointer, screen, ui,
// ui_act, session, context, ping, pause, levels, capture and record. Every position and rotation it answers is in the
// kit frame (X forward, Y right, Z up, centimetres; yaw turns right, pitch up).
//
// Three.js has no standard player, movement or UI, so the game gives them as hooks (connectBridge's `hooks`). The
// scene graph gives the things; the canvas gives the frames; keys go in as KeyboardEvents and a virtual gamepad.
//
// The module imports nothing (not even three): the math it needs is here, on the plain arrays Three.js keeps
// (matrixWorld.elements, geometry.boundingBox). A Node test drives it with plain objects (plugin/threejs/test/).
//
// The bridge also has a 2D mode (`dimension: "2d"`, makeFrame2D): plugin/phaser builds a scene of the same shape from
// a Phaser game and gives its own engine name.
//
// Licensed under the Apache License 2.0, as the rest of Autopilot.

export const PROTOCOL = 1;
export const VERSION = "0.1.0";
export const DEFAULT_URL = "ws://127.0.0.1:8765/v1/bridge";
const KEY_TAP_MS = 50;
const KEY_TAP_FRAMES = 2;
const DEFAULT_LEASE = 1.0;

// -- the kit frame -------------------------------------------------------------------------------------------------
//
// Three.js: x right, y up, -z forward (a camera looks down -z), right-handed, in the game's own unit (`metres` per
// unit, 1 by default). Kit (X, Y, Z) = scale * (-z, x, y), scale = 100 * metres: the Godot plugin's frame, as both
// engines share their axes.

export function makeFrame(metres = 1) {
  const s = 100 * metres;
  const r2 = (x) => (Number.isFinite(x) ? Math.round(x * 100) / 100 + 0 : x); // (+ 0: never -0)
  return {
    scale: s,
    dimension: "3d",
    kit: (v) => ({ X: r2(-v[2] * s), Y: r2(v[0] * s), Z: r2(v[1] * s) }),
    kitDir: (v) => ({ X: r2(-v[2]), Y: r2(v[0]), Z: r2(v[1]) }),
    kitExtent: (half) => ({ X: r2(Math.abs(half[2]) * s), Y: r2(Math.abs(half[0]) * s), Z: r2(Math.abs(half[1]) * s) }),
    fromKit: (d) => [Number(d?.Y ?? 0) / s, Number(d?.Z ?? 0) / s, -Number(d?.X ?? 0) / s],
    fromKitDir: (d) => [Number(d?.Y ?? 0), Number(d?.Z ?? 0), -Number(d?.X ?? 0)],
    // Kit yaw of a native direction: degrees about Z from +X toward +Y; pitch: up positive.
    yawOf: (d) => r2((Math.atan2(d[0], -d[2]) * 180) / Math.PI),
    pitchOf: (d) => r2((Math.atan2(d[1], Math.hypot(d[0], d[2])) * 180) / Math.PI),
    r2,
  };
}

// A 2D game (the bridge's `dimension: "2d"`), as the Godot plugin's 2D. The scene is the plane z = 0: x right, y up (a
// game whose y points down gives -y), z toward the viewer, in the game's own unit (`kitPerUnit` kit units per unit, 1
// by default: 1 pixel = 1 kit unit). Kit (X, Y, Z) = k * (x, z, y): kit X is screen right, kit Z is screen up, and kit
// Y is 0 on the plane. A rotation in the plane is a kit pitch (counterclockwise is up); a direction to the left has
// yaw 180, every other direction yaw 0.
export function makeFrame2D(kitPerUnit = 1) {
  const s = kitPerUnit;
  const r2 = (x) => (Number.isFinite(x) ? Math.round(x * 100) / 100 + 0 : x); // (+ 0: never -0)
  const deg = (r) => (r * 180) / Math.PI;
  return {
    scale: s,
    dimension: "2d",
    kit: (v) => ({ X: r2(v[0] * s), Y: r2(v[2] * s), Z: r2(v[1] * s) }),
    kitDir: (v) => ({ X: r2(v[0]), Y: r2(v[2]), Z: r2(v[1]) }),
    kitExtent: (half) => ({ X: r2(Math.abs(half[0]) * s), Y: r2(Math.abs(half[2]) * s), Z: r2(Math.abs(half[1]) * s) }),
    fromKit: (d) => [Number(d?.X ?? 0) / s, Number(d?.Z ?? 0) / s, Number(d?.Y ?? 0) / s],
    fromKitDir: (d) => [Number(d?.X ?? 0), Number(d?.Z ?? 0), Number(d?.Y ?? 0)],
    yawOf: (d) => (d[0] < -1e-9 ? 180 : 0),
    pitchOf: (d) => (Math.abs(d[0]) < 1e-9 && Math.abs(d[1]) < 1e-9 ? 0 : r2(deg(Math.atan2(d[1], Math.abs(d[0]))))),
    // An object's rotation from its world matrix: the angle of its x axis in the plane
    rotationOf: (m) => ({ Pitch: r2(deg(Math.atan2(m[1], m[0]))), Yaw: 0, Roll: 0 }),
    r2,
  };
}

// -- small math on Three.js' plain arrays ----------------------------------------------------------------------------

function apply4(m, p) {
  // m: a column-major 4x4 (Matrix4.elements); p: [x, y, z]. Returns [x, y, z, w].
  const [x, y, z] = p;
  return [
    m[0] * x + m[4] * y + m[8] * z + m[12],
    m[1] * x + m[5] * y + m[9] * z + m[13],
    m[2] * x + m[6] * y + m[10] * z + m[14],
    m[3] * x + m[7] * y + m[11] * z + m[15],
  ];
}

function worldMatrix(o) {
  return o?.matrixWorld?.elements ?? [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1];
}

export function worldPosition(o) {
  const m = worldMatrix(o);
  return [m[12], m[13], m[14]];
}

// The forward direction (-z) of an object's world matrix, normalized.
export function worldForward(o) {
  const m = worldMatrix(o);
  const d = [-m[8], -m[9], -m[10]];
  const n = Math.hypot(d[0], d[1], d[2]) || 1;
  return [d[0] / n, d[1] / n, d[2] / n];
}

// Kit rotation {Pitch, Yaw, Roll} of an object: yaw and pitch of its -z axis, roll from its x axis' height.
function kitRotation(frame, o) {
  const m = worldMatrix(o);
  if (frame.rotationOf) return frame.rotationOf(m);
  const f = worldForward(o);
  const right = [m[0], m[1], m[2]];
  const rn = Math.hypot(right[0], right[1], right[2]) || 1;
  return { Pitch: frame.pitchOf(f), Yaw: frame.yawOf(f), Roll: frame.r2((-Math.asin(Math.max(-1, Math.min(1, right[1] / rn))) * 180) / Math.PI) };
}

// The world box [min, max] of an object and what hangs under it: its geometries' bounding boxes (computed when
// missing). null when nothing under it has a geometry.
export function worldBox(o, { skip } = {}) {
  let min = [Infinity, Infinity, Infinity];
  let max = [-Infinity, -Infinity, -Infinity];
  let have = false;
  const stack = [o];
  while (stack.length) {
    const c = stack.pop();
    if (c !== o && skip && skip(c)) continue;
    if (c.visible === false) continue;
    const g = c.geometry;
    if (g && !c.isLight && !c.isCamera) {
      if (!g.boundingBox && typeof g.computeBoundingBox === "function") g.computeBoundingBox();
      const b = g.boundingBox;
      if (b && Number.isFinite(b.min?.x)) {
        const m = worldMatrix(c);
        for (const x of [b.min.x, b.max.x])
          for (const y of [b.min.y, b.max.y])
            for (const z of [b.min.z, b.max.z]) {
              const p = apply4(m, [x, y, z]);
              for (let i = 0; i < 3; i += 1) {
                if (p[i] < min[i]) min[i] = p[i];
                if (p[i] > max[i]) max[i] = p[i];
              }
            }
        have = true;
      }
    }
    for (const k of c.children ?? []) stack.push(k);
  }
  return have ? [min, max] : null;
}

// A world point on the camera's screen: {x, y} from -1 to 1 (y up), and whether it is in front and in view.
export function project(camera, p) {
  camera.updateMatrixWorld?.();
  const view = camera.matrixWorldInverse?.elements;
  const proj = camera.projectionMatrix?.elements;
  if (!view || !proj) return null;
  const v = apply4(view, p);
  if (v[2] >= 0) return { x: 0, y: 0, behind: true, inView: false, depth: -v[2] };
  const c = apply4(proj, [v[0], v[1], v[2]]);
  const x = c[0] / c[3];
  const y = c[1] / c[3];
  return { x, y, behind: false, inView: Math.abs(x) <= 1 && Math.abs(y) <= 1, depth: -v[2] };
}

// -- key names (Unreal FKey names, what the kit presses) -------------------------------------------------------------

const NAMED_CODES = {
  SpaceBar: "Space", Enter: "Enter", Escape: "Escape", Tab: "Tab", BackSpace: "Backspace",
  LeftShift: "ShiftLeft", RightShift: "ShiftRight", LeftControl: "ControlLeft", RightControl: "ControlRight",
  LeftAlt: "AltLeft", RightAlt: "AltRight", LeftCommand: "MetaLeft", RightCommand: "MetaRight",
  Up: "ArrowUp", Down: "ArrowDown", Left: "ArrowLeft", Right: "ArrowRight",
  Delete: "Delete", Insert: "Insert", Home: "Home", End: "End", PageUp: "PageUp", PageDown: "PageDown",
  CapsLock: "CapsLock", Pause: "Pause", Semicolon: "Semicolon", Equals: "Equal", Comma: "Comma", Hyphen: "Minus",
  Period: "Period", Slash: "Slash", Tilde: "Backquote", LeftBracket: "BracketLeft", Backslash: "Backslash",
  RightBracket: "BracketRight", Apostrophe: "Quote",
};
const DIGITS = ["Zero", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine"];
DIGITS.forEach((d, i) => {
  NAMED_CODES[d] = `Digit${i}`;
  NAMED_CODES[`NumPad${d}`] = `Numpad${i}`;
});
const MOUSE_BUTTONS = { LeftMouseButton: 0, MiddleMouseButton: 1, RightMouseButton: 2, ThumbMouseButton: 3, ThumbMouseButton2: 4 };
// Standard gamepad mapping (https://w3c.github.io/gamepad/#remapping)
const PAD_BUTTONS = {
  Gamepad_FaceButton_Bottom: 0, Gamepad_FaceButton_Right: 1, Gamepad_FaceButton_Left: 2, Gamepad_FaceButton_Top: 3,
  Gamepad_LeftShoulder: 4, Gamepad_RightShoulder: 5, Gamepad_LeftTrigger: 6, Gamepad_RightTrigger: 7,
  Gamepad_Special_Left: 8, Gamepad_Special_Right: 9, Gamepad_LeftThumbstick: 10, Gamepad_RightThumbstick: 11,
  Gamepad_DPad_Up: 12, Gamepad_DPad_Down: 13, Gamepad_DPad_Left: 14, Gamepad_DPad_Right: 15,
};
// Axis keys: [axis, sign]. Unreal's Y axes are positive up, the Gamepad API's positive down.
const PAD_AXES = { Gamepad_LeftX: [0, 1], Gamepad_LeftY: [1, -1], Gamepad_RightX: [2, 1], Gamepad_RightY: [3, -1] };
const PAD_TRIGGER_AXES = { Gamepad_LeftTriggerAxis: 6, Gamepad_RightTriggerAxis: 7 };

// A key name as a spec: {kind: "key", code} | {kind: "mouse_button", button} | {kind: "wheel", dy} |
// {kind: "mouse_axis", axis} | {kind: "pad_button", button} | {kind: "pad_axis", axis, sign} | null.
export function keySpec(name) {
  if (!name) return null;
  if (/^[A-Za-z]$/.test(name)) return { kind: "key", code: `Key${name.toUpperCase()}` };
  if (NAMED_CODES[name]) return { kind: "key", code: NAMED_CODES[name] };
  const f = /^F(\d{1,2})$/.exec(name);
  if (f && +f[1] >= 1 && +f[1] <= 12) return { kind: "key", code: name };
  if (name in MOUSE_BUTTONS) return { kind: "mouse_button", button: MOUSE_BUTTONS[name] };
  if (name === "MouseScrollUp") return { kind: "wheel", dy: -100 };
  if (name === "MouseScrollDown") return { kind: "wheel", dy: 100 };
  if (name === "MouseX" || name === "MouseY") return { kind: "mouse_axis", axis: name };
  if (name in PAD_BUTTONS) return { kind: "pad_button", button: PAD_BUTTONS[name] };
  if (name in PAD_AXES) return { kind: "pad_axis", axis: PAD_AXES[name][0], sign: PAD_AXES[name][1] };
  if (name in PAD_TRIGGER_AXES) return { kind: "pad_button", button: PAD_TRIGGER_AXES[name], analog: true };
  // A KeyboardEvent.code as it is (KeyW, ShiftLeft, ArrowUp)
  if (/^(Key[A-Z]|Digit\d|Numpad\w+|Arrow\w+|Shift\w+|Control\w+|Alt\w+|Meta\w+|Space|Enter|Escape|Tab|Backspace)$/.test(name))
    return { kind: "key", code: name };
  return null;
}

// The Unreal name of a KeyboardEvent.code (for the game's controls), or the code itself.
export function unrealName(code) {
  const k = /^Key([A-Z])$/.exec(code);
  if (k) return k[1];
  // (RightShift, RightControl... are not the arrow key Right)
  for (const [name, c] of Object.entries(NAMED_CODES)) if (c === code && (name === "Right" || !name.startsWith("Right")) && !name.startsWith("NumPad")) return name;
  if (code === "ShiftRight") return "RightShift";
  return code;
}

// The `key` text a KeyboardEvent carries for a code.
function keyText(code) {
  const k = /^Key([A-Z])$/.exec(code);
  if (k) return k[1].toLowerCase();
  const d = /^Digit(\d)$/.exec(code);
  if (d) return d[1];
  return { Space: " ", ArrowUp: "ArrowUp", ArrowDown: "ArrowDown", ArrowLeft: "ArrowLeft", ArrowRight: "ArrowRight", ShiftLeft: "Shift", ShiftRight: "Shift", ControlLeft: "Control", ControlRight: "Control", AltLeft: "Alt", AltRight: "Alt" }[code] ?? code;
}

// -- the virtual gamepad ---------------------------------------------------------------------------------------------

// A standard gamepad the bridge holds buttons and sticks on. navigator.getGamepads() lists it first while it is on
// (installGamepad), so a game that polls the Gamepad API sees it as any pad.
export function createVirtualPad() {
  const buttons = Array.from({ length: 17 }, () => ({ pressed: false, touched: false, value: 0 }));
  const axes = [0, 0, 0, 0];
  return {
    id: "Autopilot virtual gamepad (STANDARD GAMEPAD)",
    index: 0,
    connected: true,
    mapping: "standard",
    timestamp: 0,
    buttons,
    axes,
    active: false,
    setButton(i, value) {
      buttons[i] = { pressed: value > 0.5, touched: value > 0, value };
      this.active = true;
      this.timestamp += 1;
    },
    setAxis(i, value) {
      axes[i] = Math.max(-1, Math.min(1, value));
      this.active = true;
      this.timestamp += 1;
    },
    idle() {
      return buttons.every((b) => b.value === 0) && axes.every((a) => a === 0);
    },
  };
}

function installGamepad(nav, pad) {
  if (!nav || typeof nav.getGamepads !== "function" || nav.__autopilotPad) return () => {};
  const original = nav.getGamepads.bind(nav);
  nav.__autopilotPad = pad;
  nav.getGamepads = () => {
    const real = Array.from(original() ?? []);
    if (!pad.active) return real;
    return [pad, ...real.filter(Boolean)];
  };
  return () => {
    nav.getGamepads = original;
    delete nav.__autopilotPad;
  };
}

// -- errors ----------------------------------------------------------------------------------------------------------

// Whether an address is the agent service on this machine (ws:// or wss:// to 127.0.0.0/8, localhost or ::1). The
// bridge lets whatever it connects to read the game, press its keys and call its functions: a link with another host
// in it must not take a player's game over, so other hosts need the game's own allowRemote.
export function isLoopbackUrl(address) {
  let u;
  try {
    u = new URL(String(address));
  } catch {
    return false;
  }
  if (u.protocol !== "ws:" && u.protocol !== "wss:") return false;
  const host = u.hostname.replace(/^\[|\]$/g, "").toLowerCase();
  return host === "localhost" || host === "::1" || /^127(\.\d{1,3}){3}$/.test(host);
}

export class OpError extends Error {
  constructor(message, status = 400) {
    super(message);
    this.status = status;
  }
}

// -- the bridge ------------------------------------------------------------------------------------------------------

// connectBridge(options) -> bridge. options:
//   url: the agent service's address (DEFAULT_URL): this machine only (isLoopbackUrl) unless allowRemote is true;
//   WebSocket: the WebSocket class (globalThis.WebSocket);
//   scene, camera: the game's THREE.Scene and the camera it renders with (a function returning it, if it changes);
//   canvas: the canvas it renders to (frames, the pointer, mouse events); keyTarget: what keyboard events go to
//   (window);
//   project: the game's name (hello's `project`); projectDir: the project folder on this machine, when the game knows it;
//   metres: metres per scene unit (1);
//   dimension: "3d" (the default) or "2d": a 2D game's scene is the plane z = 0, x right and y up (makeFrame2D); then
//   kitPerUnit: kit units per scene unit (1: 1 pixel = 1 kit unit, as the Godot plugin's 2D);
//   engine, engineVersion: what hello and context say (default "threejs" and three's revision): a helper for another
//   engine (plugin/phaser) builds a scene of this shape and gives its own name;
//   moveKeys: {up, down, left, right}, the keys `move` holds without a move hook (W, S, A, D; in 3D up is forward);
//   thingDepth: how deep under the scene its things are (2: the scene's direct children);
//   pictureCanvas: the canvas frames and stills read (the canvas by default), e.g. a copy the game keeps of each frame;
//   nodeOf(value) -> scene object | null: the scene object a game value stands for, so `rc` answers its path. A scene
//   object may carry `rcTarget`: the game's own object that `rc` reads and calls in its place;
//   hooks: what Three.js has no standard for (all optional except player):
//     player() -> {object?: Object3D, path?: string, position: [x,y,z], velocity?: [x,y,z], forward?: [x,y,z],
//                  onGround?: bool, halfHeight?: number (scene units)} | null: the player now;
//     movement() -> {walkSpeed?, jumpSpeed?, gravity?, halfHeight?, radius?, step?, maxSlope?, airControl?} in scene
//                  units and degrees;
//     controls() -> [{name, keys: [KeyboardEvent.code or Unreal names], digital?}]: the game's input actions;
//     restart({map}) -> Promise|void: start the level over (or another one); levels() -> {levels, playing};
//     pause(paused) -> bool: pause or resume, answering whether the game is paused; paused() -> bool;
//     gameTime() -> seconds of game time (wall time since connect by default);
//     move({direction: native [x,y,z], seconds, scale, keys}) -> Promise<{moved?}>: walk a direction (default: the
//                  keys W/A/S/D held camera-relative); keys: {hold(name), release(name)};
//     render(): render one frame now, so the canvas holds a picture to read (frames, stills);
//     uiRoot: the DOM element the game's UI is under (document.body);
//     objects: {name: object} the `rc` reads and calls reach by name (game.get("state", "score"));
//     classOf(object) -> string: an object's class for `find` (default: userData.class, else its type).
export function connectBridge(options = {}) {
  const bridge = createBridge(options);
  bridge.connect();
  return bridge;
}

export function createBridge(options = {}) {
  const hooks = options.hooks ?? {};
  const dimension = options.dimension === "2d" ? "2d" : "3d";
  const frame = dimension === "2d" ? makeFrame2D(options.kitPerUnit ?? 1) : makeFrame(options.metres ?? 1);
  const engine = String(options.engine ?? "threejs");
  const moveKeys = { up: "W", down: "S", left: "A", right: "D", ...(options.moveKeys ?? {}) };
  const g = options.global ?? globalThis;
  const doc = options.document ?? g.document;
  const keyTarget = options.keyTarget ?? g;
  const WS = options.WebSocket ?? g.WebSocket;
  const url = options.url ?? DEFAULT_URL;
  const nowMs = options.now ?? (() => (g.performance ? g.performance.now() : Date.now()));
  const raf = options.requestFrame ?? ((fn) => (g.requestAnimationFrame ? g.requestAnimationFrame(fn) : setTimeout(() => fn(nowMs()), 16)));
  const startedAt = nowMs();
  const pad = createVirtualPad();
  const uninstallPad = installGamepad(g.navigator, pad);

  const scene = () => (typeof options.scene === "function" ? options.scene() : options.scene);
  const camera = () => (typeof options.camera === "function" ? options.camera() : options.camera);
  const canvas = () => (typeof options.canvas === "function" ? options.canvas() : options.canvas);
  const pictureCanvas = () => {
    const p = typeof options.pictureCanvas === "function" ? options.pictureCanvas() : options.pictureCanvas;
    return p ?? canvas();
  };
  const thingDepth = Number(options.thingDepth ?? 2);

  let ws = null;
  let state = "idle";
  let session = null;
  let backoff = 1000;
  let closed = false;
  let lease = DEFAULT_LEASE;
  let pointer = null; // the canvas' CSS pixels from its top left, null: its middle
  let frames = 0;
  let events = [];
  const held = new Map(); // key name -> {spec, value, renewed (ms), until (game seconds) }
  const deferred = []; // per-frame tickers: fn(now) -> true when done
  let recording = null;
  let heartbeatAt = 0;
  let sceneVersion = 0;

  const gameTime = () => (hooks.gameTime ? Number(hooks.gameTime()) : (nowMs() - startedAt) / 1000);
  const t = () => frame.r2(gameTime());

  // -- the link ------------------------------------------------------------------------------------------------------

  function send(obj) {
    if (ws && ws.readyState === 1) ws.send(typeof obj === "string" ? obj : JSON.stringify(obj));
  }

  function hello() {
    return {
      type: "hello",
      protocol: PROTOCOL,
      plugin_version: VERSION,
      engine,
      engine_version: String(options.engineVersion ?? options.threeRevision ?? g.__THREE__ ?? ""),
      project: options.project ?? (doc?.title || "web game"),
      host: "game",
      features: ["timed_hold", "levels", "pointer", "media"],
      // Never share runs' data from a browser game: this bridge has no setting for it, so it says no
      share: false,
      input: inputCaps(),
      ...(session ? { resume: session } : {}),
    };
  }

  function connect() {
    if (closed || !WS) return;
    if (!options.allowRemote && !isLoopbackUrl(url)) {
      state = "refused";
      options.onStatus?.(`refused: ${url} is not on this machine (the game must pass allowRemote to connect there)`);
      return;
    }
    state = "connecting";
    try {
      ws = new WS(url);
    } catch (e) {
      state = "error";
      retry();
      return;
    }
    ws.onopen = () => send(hello());
    ws.onmessage = (m) => {
      let f;
      try {
        f = JSON.parse(typeof m.data === "string" ? m.data : String(m.data));
      } catch {
        return;
      }
      onFrame(f);
    };
    ws.onclose = () => {
      ws = null;
      if (state === "connected") emitLocal("disconnected");
      state = closed ? "closed" : "connecting";
      releaseAll();
      stopRecording();
      retry();
    };
    ws.onerror = () => {};
  }

  function retry() {
    if (closed) return;
    setTimeout(connect, backoff);
    backoff = Math.min(backoff * 2, 15000);
  }

  function emitLocal(what) {
    options.onStatus?.(what);
  }

  function onFrame(f) {
    switch (f.type) {
      case "welcome":
        state = "connected";
        session = f.session ?? session;
        backoff = 1000;
        emitLocal("connected");
        break;
      case "request":
        handleRequest(f);
        break;
      case "status":
        options.onStatus?.(`status: ${f.text ?? ""}`);
        break;
      case "result":
        options.onResult?.(f);
        break;
      case "error":
        options.onStatus?.(`error: ${f.message ?? ""}`);
        break;
      default:
        break;
    }
  }

  async function handleRequest(f) {
    const id = f.id;
    try {
      const result = await handle(String(f.op ?? ""), f.args && typeof f.args === "object" ? f.args : {}, id);
      if (result === RESPONDED) return;
      send({ type: "response", id, ok: true, result: result ?? {}, t: t() });
    } catch (e) {
      send({ type: "response", id, ok: false, status: e instanceof OpError ? e.status : 500, error: String(e?.message ?? e), t: t() });
    }
  }

  const RESPONDED = Symbol("responded");

  // -- the frame loop: deferred answers, held keys, heartbeats, recording ---------------------------------------------

  function tick(now) {
    frames += 1;
    for (let i = deferred.length - 1; i >= 0; i -= 1) {
      let done = true;
      try {
        done = deferred[i](now);
      } catch (e) {
        done = true;
      }
      if (done) deferred.splice(i, 1);
    }
    tickKeys(now);
    if (state === "connected") {
      if (now - heartbeatAt >= 1000) {
        heartbeatAt = now;
        events.push({ kind: "heartbeat", t: t(), fps: null, paused: Boolean(hooks.paused?.()) });
      }
      if (recording && now >= recording.next) {
        recording.next = now + 1000 / recording.fps;
        const pic = picture(recording.maxDim, recording.quality);
        if (pic) {
          recording.n += 1;
          recording.sent += 1;
          send({ type: "frame", n: recording.n, t: t(), wall: Date.now() / 1000, width: pic.width, height: pic.height, jpeg: pic.jpeg });
        }
      }
      if (events.length) {
        send({ type: "events", events, t: t() });
        events = [];
      }
    } else {
      events = [];
    }
    if (!closed) raf(tick);
  }

  // -- ops -----------------------------------------------------------------------------------------------------------

  async function handle(op, args, id) {
    switch (op) {
      case "ping":
        return { game_time: t(), wall_time: Date.now() / 1000 };
      case "context":
        return context();
      case "session":
        return sessionOp(args);
      case "levels":
        return levelsOp();
      case "pause": {
        const paused = hooks.pause ? Boolean(hooks.pause(Boolean(args.paused))) : false;
        return hooks.pause ? { paused } : { paused: false, why: "the game gives no pause hook" };
      }
      case "find":
        return { objects: find(args) };
      case "transforms":
        return transformsOp(args);
      case "pose":
        return poseOp();
      case "movement":
        return movementOp();
      case "view":
        return viewOp(args);
      case "watch":
        return watchOp(args);
      case "key":
        return keyOp(args);
      case "input":
        return inputOp(args);
      case "move":
        return moveOp(args);
      case "pointer":
        return pointerOp(args);
      case "screen":
        return screenOp(args);
      case "ui":
        return { elements: uiList() };
      case "ui_act":
        return uiAct(args);
      case "rc":
        return rcOp(String(args.verb ?? ""), args.body);
      case "capture": {
        const pic = picture(clampDim(args.max_dim, 1920), clampQuality(args.quality, 90));
        if (!pic) throw new OpError("no picture: the canvas can't be read", 409);
        send(`{"type":"response","id":${JSON.stringify(id)},"ok":true,"result":{"width":${pic.width},"height":${pic.height},"t":${t()},"jpeg":"${pic.jpeg}"},"t":${t()}}`);
        return RESPONDED;
      }
      case "record":
        if (args.action === "start") return startRecording(args);
        if (args.action === "stop") return stopRecording();
        throw new OpError("record wants action start or stop");
      case "timestep":
        return { note: "a browser game's frame time is the display's: the bridge can't fix it" };
      case "detectors":
        return {};
      default:
        throw new OpError(`unknown op ${op}`);
    }
  }

  function inputCaps() {
    return {
      handling: "browser",
      keys: true,
      actions: Boolean(hooks.controls),
      pointer: true,
      move: hooks.move ? "game" : "keys",
      notes: [
        "Keys go in as KeyboardEvents (code and key) on the page; gamepad keys (Gamepad_*) on a virtual standard gamepad that navigator.getGamepads() lists first while it holds anything.",
        "Mouse buttons and MouseX/MouseY go in as pointer and mouse events on the canvas; the pointer is never locked.",
      ],
    };
  }

  function context() {
    const p = playerNow();
    const cam = camera();
    return {
      world: scenePath(), scene: scenePath(), character: p ? p.path : "", controller: "", input_subsystem: "",
      camera_manager: cam ? pathOf(cam) : "", camera: cam ? pathOf(cam) : "", dimension,
      game_name: options.project ?? (doc?.title || ""), host: "game", engine,
      session_running: Boolean(scene()), paused: Boolean(hooks.paused?.()), game_time: t(),
      project_dir: options.projectDir ?? "", controls: controls(), input: inputCaps(), net: { mode: "standalone" },
      page: g.location ? String(g.location.href) : "",
    };
  }

  function controls() {
    if (!hooks.controls) return [];
    return (hooks.controls() ?? []).map((a) => ({
      name: a.name, action: a.name, keys: (a.keys ?? []).map(unrealName), web_keys: a.keys ?? [], digital: a.digital ?? true,
    }));
  }

  async function sessionOp(args) {
    const action = String(args.action ?? "status");
    if (action === "status" || (action === "start" && !args.map)) return { running: Boolean(scene()), scene: scenePath() };
    if (action === "stop") return { running: Boolean(scene()), scene: scenePath(), note: "a browser game stops when its page closes" };
    if (!hooks.restart) throw new OpError("the game gives no restart hook", 501);
    releaseAll();
    await hooks.restart({ map: args.map ?? null, fresh: Boolean(args.fresh) });
    sceneVersion += 1;
    events.push({ kind: "session", t: t(), running: true, scene: scenePath() });
    return { running: true, scene: scenePath() };
  }

  function levelsOp() {
    if (hooks.levels) {
      const l = hooks.levels() ?? {};
      return { levels: l.levels ?? [], open: l.playing ?? scenePath(), playing: l.playing ?? scenePath() };
    }
    return { levels: [scenePath()], open: scenePath(), playing: scenePath() };
  }

  // -- things: paths in the scene graph ----------------------------------------------------------------------------

  // A thing's path: its names from the scene down, "/" between them. An unnamed object is named by its type; a name
  // that repeats among its siblings gets "#2", "#3"... (in their order).
  function segments(parent) {
    const seen = new Map();
    return (parent.children ?? []).map((c) => {
      const base = (c.name || c.type || "Object3D").replace(/\//g, "_");
      const n = (seen.get(base) ?? 0) + 1;
      seen.set(base, n);
      return [n === 1 ? base : `${base}#${n}`, c];
    });
  }

  function scenePath() {
    const s = scene();
    return s ? `/${(s.name || "Scene").replace(/\//g, "_")}` : "";
  }

  // Every object under the scene with its path, depth first.
  function walk(fn) {
    const s = scene();
    if (!s) return;
    const stack = [[scenePath(), s]];
    while (stack.length) {
      const [path, o] = stack.pop();
      if (fn(path, o) === false) return;
      const kids = segments(o);
      for (let i = kids.length - 1; i >= 0; i -= 1) stack.push([`${path}/${kids[i][0]}`, kids[i][1]]);
    }
  }

  function pathOf(target) {
    let out = "";
    walk((path, o) => {
      if (o === target) {
        out = path;
        return false;
      }
      return true;
    });
    return out;
  }

  function resolve(path) {
    const s = scene();
    if (!s || typeof path !== "string") return null;
    const root = scenePath();
    if (path === root) return s;
    if (!path.startsWith(`${root}/`)) return null;
    let o = s;
    for (const part of path.slice(root.length + 1).split("/")) {
      const hit = segments(o).find(([name]) => name === part);
      if (!hit) return null;
      o = hit[1];
    }
    return o;
  }

  function classOf(o) {
    if (hooks.classOf) {
      const c = hooks.classOf(o);
      if (c) return String(c);
    }
    return String(o.userData?.class ?? o.type ?? "Object3D");
  }

  // Whether an object is of a class: its class (classOf), its type, or a type it inherits (isMesh for "Mesh").
  function isOf(o, cls) {
    if (classOf(o) === cls || o.type === cls) return true;
    if (o[`is${cls}`] === true) return true;
    let proto = Object.getPrototypeOf(o);
    while (proto && proto.constructor && proto.constructor !== Object) {
      if (proto.constructor.name === cls) return true;
      proto = Object.getPrototypeOf(proto);
    }
    return false;
  }

  function isThing(path, o) {
    // The scene's things, as Unreal's actors: the scene's direct children and what the game marks as one
    // (userData.thing, or a class of its own: userData.class or the classOf hook's)
    const depth = path.split("/").length - 1;
    return depth === thingDepth || o.userData?.thing === true || o.userData?.class !== undefined || Boolean(hooks.classOf?.(o));
  }

  function find(args) {
    const cls = String(args.class ?? "");
    const name = String(args.name ?? "").toLowerCase();
    const limit = Number(args.limit ?? 10000);
    const out = [];
    walk((path, o) => {
      if (out.length >= limit) return false;
      if (o === scene() && (cls || name || args.things)) return true;
      if (args.things && !isThing(path, o)) return true;
      if (cls && !isOf(o, cls)) return true;
      if (name && !String(o.name ?? "").toLowerCase().includes(name)) return true;
      out.push(path);
      return true;
    });
    if (cls && out.length === 0) {
      let known = false;
      walk((_, o) => {
        if (isOf(o, cls)) known = true;
        return !known;
      });
      // (a class nothing is, as Godot's: an error, not an empty list; a known one with none left answers [])
      if (!known && !/^(Mesh|Group|Object3D|Points|Line|Sprite|Light|Camera|SkinnedMesh|InstancedMesh)$/.test(cls))
        throw new OpError(`Unknown class ${cls} (no object of that type or userData.class in the scene)`, 400);
    }
    return out;
  }

  function activeOf(o) {
    let c = o;
    while (c) {
      if (c.visible === false) return false;
      c = c.parent;
    }
    return true;
  }

  function transformOf(o) {
    const p = playerNow();
    const isPlayer = p && p.object === o;
    const box = worldBox(o);
    const at = isPlayer ? p.position : worldPosition(o);
    const center = box ? box[0].map((v, i) => (v + box[1][i]) / 2) : at;
    const half = box ? box[0].map((v, i) => (box[1][i] - v) / 2) : [0, 0, 0];
    return {
      location: frame.kit(at),
      rotation: kitRotation(frame, o),
      velocity: frame.kit(isPlayer && p.velocity ? p.velocity : [0, 0, 0]),
      center: frame.kit(center),
      extent: frame.kitExtent(half),
      active: activeOf(o),
    };
  }

  function transformsOp(args) {
    const out = {};
    for (const path of args.objects ?? []) {
      const o = resolve(String(path));
      out[String(path)] = o ? transformOf(o) : null;
    }
    return { objects: out };
  }

  // -- the player --------------------------------------------------------------------------------------------------

  function playerNow() {
    const p = hooks.player?.();
    if (!p) return null;
    const object = p.object ?? (p.path ? resolve(p.path) : null);
    const position = p.position ?? (object ? worldPosition(object) : null);
    if (!position) return null;
    return { ...p, object, position, path: p.path ?? (object ? pathOf(object) : "") };
  }

  function poseOp() {
    const p = playerNow();
    if (!p) throw new OpError("No player", 404);
    const cam = camera();
    const hh = Number(p.halfHeight ?? 0);
    const center = [p.position[0], p.position[1] + hh, p.position[2]];
    const fwd = cam ? worldForward(cam) : p.forward ?? [0, 0, -1];
    const eye = cam ? eyeOf(cam) : center;
    return {
      path: p.path, at: frame.kit(center), feet: frame.kit(p.position), eye: frame.kit(eye),
      yaw: frame.yawOf(fwd), pitch: frame.pitchOf(fwd), velocity: frame.kit(p.velocity ?? [0, 0, 0]),
      on_ground: p.onGround ?? null,
      ...(p.forward ? { body_yaw: frame.yawOf(p.forward) } : {}),
      ...(p.state !== undefined ? { state: p.state } : {}),
    };
  }

  function movementOp() {
    if (!playerNow()) throw new OpError("No player", 404);
    const m = hooks.movement?.() ?? {};
    const s = frame.scale;
    const num = (v, k = s) => (Number.isFinite(v) ? frame.r2(v * k) : null);
    return {
      walk_speed: num(m.walkSpeed), jump_z: num(m.jumpSpeed), gravity: num(m.gravity !== undefined ? Math.abs(m.gravity) : undefined),
      half_height: num(m.halfHeight), radius: num(m.radius), step: num(m.step), max_slope: num(m.maxSlope, 1),
      air_control: num(m.airControl, 1), sources: { all: hooks.movement ? "the game's movement hook" : "none" },
      ...(m.extra ? { extra: m.extra } : {}),
    };
  }

  // -- view and watch ------------------------------------------------------------------------------------------------

  function viewOp(args) {
    const cam = camera();
    const s = scene();
    if (!cam || !s) return { visible: [], camera: null, body: null, effects_play: true, omitted: OMITTED };
    const only = Array.isArray(args.only) ? new Set(args.only.map(String)) : null;
    const maxCount = Number(args.max_count ?? 40);
    const p = playerNow();
    const eye = eyeOf(cam);
    const items = [];
    walk((path, o) => {
      if (o === s) return true;
      // things: as find's `things`, never the player's body
      if (!isThing(path, o)) return true;
      if (p && p.object && (o === p.object || isAncestor(p.object, o))) return true;
      const name = o.name || segments(o.parent ?? {}).find(([, c]) => c === o)?.[0] || o.type;
      if (only && !only.has(name) && !only.has(path)) return true;
      if (!activeOf(o)) return true;
      const box = worldBox(o);
      if (!box) return true;
      const center = box[0].map((v, i) => (v + box[1][i]) / 2);
      const half = box[0].map((v, i) => (box[1][i] - v) / 2);
      const wide = dimension === "2d" ? half[0] : Math.max(half[0], half[2]);
      if (!only && wide * frame.scale * 2 > Number(args.floor_size ?? 4000) && half[1] * frame.scale < 50) return true;
      const sp = project(cam, center);
      const corners = [];
      for (const x of [box[0][0], box[1][0]]) for (const y of [box[0][1], box[1][1]]) for (const z of [box[0][2], box[1][2]]) corners.push(project(cam, [x, y, z]));
      const partly = corners.some((c) => c && c.inView);
      if (!sp || (!sp.inView && !partly)) return true;
      const dist = Math.hypot(center[0] - eye[0], center[1] - eye[1], center[2] - eye[2]) * frame.scale;
      const item = {
        name, path, class: classOf(o), center: frame.kit(center), size: frame.kitExtent(half.map((h) => h * 2)),
        distance: frame.r2(dist), screen_x: frame.r2(sp.x), screen_y: frame.r2(sp.y),
        crosshair_on: Math.abs(sp.x) < 0.05 && Math.abs(sp.y) < 0.05,
      };
      if (!sp.inView) item.partly_in_view = true;
      if (p) {
        item.top = frame.r2((box[1][1] - p.position[1]) * frame.scale);
        item.bottom = frame.r2((box[0][1] - p.position[1]) * frame.scale);
      }
      if (o.userData?.label) item.label = String(o.userData.label);
      items.push(item);
      return true;
    });
    items.sort((a, b) => a.distance - b.distance);
    const fwd = worldForward(cam);
    const vfov = Number(cam.fov ?? 60);
    const aspect = Number(cam.aspect ?? 1);
    const hfov = (2 * Math.atan(Math.tan((vfov * Math.PI) / 360) * aspect) * 180) / Math.PI;
    // (2D: the camera's view is a rectangle, its width and height in kit units, not a field of view)
    const camOut = dimension === "2d"
      ? { location: frame.kit(eye), yaw: 0, pitch: 0, width: frame.r2(Number(cam.viewWidth ?? 0) * frame.scale), height: frame.r2(Number(cam.viewHeight ?? 0) * frame.scale) }
      : { location: frame.kit(eye), yaw: frame.yawOf(fwd), pitch: frame.pitchOf(fwd), fov: frame.r2(hfov), vfov: frame.r2(vfov) };
    const out = {
      visible: items.slice(0, maxCount),
      camera: camOut,
      crosshair_tested: false, oriented: false, effects_play: true,
      body: p ? { on_ground: p.onGround ?? null, falling: p.onGround === false && (p.velocity?.[1] ?? 0) < 0, ...(p.state !== undefined ? { state: p.state } : {}) } : null,
      omitted: OMITTED,
    };
    if (only) out.only = [...only];
    return out;
  }

  // Where the camera sees from: its position; in 2D its middle on the plane (z = 0), so distances are in the plane.
  function eyeOf(cam) {
    const e = worldPosition(cam);
    return dimension === "2d" ? [e[0], e[1], 0] : e;
  }

  function isAncestor(a, o) {
    let c = o.parent;
    while (c) {
      if (c === a) return true;
      c = c.parent;
    }
    return false;
  }

  function watchOp(args) {
    const paths = (args.objects ?? []).map(String);
    const seconds = Number(args.seconds ?? 0.6);
    const start = new Map();
    for (const path of paths) {
      const o = resolve(path);
      start.set(path, o ? transformOf(o) : null);
    }
    const found = new Map();
    const sample = () => {
      for (const path of paths) {
        const first = start.get(path);
        const o = resolve(path);
        const now = o ? transformOf(o) : null;
        const note = (what) => {
          const key = `${path}|${what}`;
          if (!found.has(key)) found.set(key, { t: t(), object: path, what });
        };
        if (first && !now) note("gone");
        else if (!first && now) note("appeared");
        else if (first && now) {
          const d = Math.hypot(now.location.X - first.location.X, now.location.Y - first.location.Y, now.location.Z - first.location.Z);
          if (d > 1) note("moved");
          if (Math.abs(now.rotation.Yaw - first.rotation.Yaw) > 1 || Math.abs(now.rotation.Pitch - first.rotation.Pitch) > 1) note("turned");
          if (Math.abs(now.extent.X - first.extent.X) + Math.abs(now.extent.Y - first.extent.Y) + Math.abs(now.extent.Z - first.extent.Z) > 1) note("size");
          if (now.active !== first.active) note(now.active ? "shown" : "hidden");
        }
      }
    };
    return new Promise((resolveP) => {
      const until = nowMs() + seconds * 1000;
      deferred.push((now) => {
        sample();
        if (now < until) return false;
        resolveP({ changes: [...found.values()].sort((a, b) => a.t - b.t) });
        return true;
      });
    });
  }

  // -- the player's hands ------------------------------------------------------------------------------------------

  function dispatchKey(code, down) {
    const Ev = g.KeyboardEvent;
    const init = { code, key: keyText(code), bubbles: true, cancelable: true };
    const ev = Ev ? new Ev(down ? "keydown" : "keyup", init) : { type: down ? "keydown" : "keyup", ...init, preventDefault() {} };
    keyTarget.dispatchEvent?.(ev);
  }

  function canvasRect() {
    const c = canvas();
    const r = c?.getBoundingClientRect?.();
    return r ?? { left: 0, top: 0, width: c?.clientWidth ?? 1, height: c?.clientHeight ?? 1 };
  }

  function pointerAt() {
    const r = canvasRect();
    const p = pointer ?? [r.width / 2, r.height / 2];
    return { clientX: r.left + p[0], clientY: r.top + p[1] };
  }

  const buttonsDown = new Set();

  function dispatchMouse(type, extra = {}) {
    const c = canvas();
    if (!c?.dispatchEvent) return;
    const at = pointerAt();
    const init = { bubbles: true, cancelable: true, ...at, buttons: [...buttonsDown].reduce((m, b) => m | (1 << b), 0), ...extra };
    const pointerType = type.startsWith("pointer");
    const Ev = type === "wheel" ? g.WheelEvent : pointerType ? g.PointerEvent ?? g.MouseEvent : g.MouseEvent;
    const ev = Ev ? new Ev(type, pointerType ? { pointerId: 1, pointerType: "mouse", isPrimary: true, ...init } : init) : { type, ...init };
    c.dispatchEvent(ev);
  }

  function send1(spec, down, value = 1) {
    switch (spec.kind) {
      case "key":
        dispatchKey(spec.code, down);
        break;
      case "mouse_button":
        if (down) buttonsDown.add(spec.button);
        else buttonsDown.delete(spec.button);
        dispatchMouse(down ? "pointerdown" : "pointerup", { button: spec.button });
        dispatchMouse(down ? "mousedown" : "mouseup", { button: spec.button });
        if (!down && spec.button === 0) dispatchMouse("click", { button: 0 });
        break;
      case "wheel":
        if (down) dispatchMouse("wheel", { deltaY: spec.dy * Math.abs(value || 1) });
        break;
      case "mouse_axis": {
        // Unreal's MouseY is positive up, the browser's movementY positive down
        const mx = spec.axis === "MouseX" ? value : 0;
        const my = spec.axis === "MouseY" ? -value : 0;
        dispatchMouse("pointermove", { movementX: mx, movementY: my });
        dispatchMouse("mousemove", { movementX: mx, movementY: my });
        break;
      }
      case "pad_button":
        pad.setButton(spec.button, down ? Math.max(0, Math.min(1, Math.abs(value))) : 0);
        break;
      case "pad_axis":
        pad.setAxis(spec.axis, down ? value * spec.sign : 0);
        break;
      default:
        break;
    }
    if (pad.active && pad.idle()) pad.active = false;
  }

  const isAxis = (spec) => spec.kind === "mouse_axis" || spec.kind === "pad_axis";

  // Holds whose lease ran out (or whose timed hold ended) let go; held axes move each frame.
  function tickKeys(now) {
    for (const [name, h] of [...held]) {
      const over = h.until > 0 ? gameTime() >= h.until : now - h.renewed > lease * 1000;
      if (over) {
        if (!isAxis(h.spec) || h.spec.kind === "pad_axis") send1(h.spec, false);
        held.delete(name);
      } else if (h.spec.kind === "mouse_axis") {
        send1(h.spec, true, h.rate ? h.value / 60 : h.value);
      }
    }
  }

  function releaseAll() {
    for (const [, h] of held) if (!isAxis(h.spec) || h.spec.kind === "pad_axis") send1(h.spec, false);
    held.clear();
  }

  // Taps that wait a moment and a couple of frames before letting go: a game that samples input once a fixed tick
  // misses a shorter one.
  function tap(spec, value, isHeld) {
    send1(spec, true, value);
    const started = nowMs();
    const f0 = frames;
    return new Promise((resolveP) => {
      deferred.push((now) => {
        if (now - started < KEY_TAP_MS || frames < f0 + KEY_TAP_FRAMES) return false;
        if (!isHeld()) send1(spec, false);
        resolveP({});
        return true;
      });
    });
  }

  function keyOp(args) {
    const mode = String(args.mode ?? "press");
    if (args.lease !== undefined && Number(args.lease) > 0) lease = Math.max(0.2, Math.min(10, Number(args.lease)));
    if (mode === "release_all") {
      releaseAll();
      return {};
    }
    const name = String(args.key ?? "");
    const spec = keySpec(name);
    if (!spec) throw new OpError(`Unknown key ${name}`);
    const value = Number(args.value ?? 1);
    if (mode === "release") {
      const h = held.get(name);
      if (h) {
        if (!isAxis(spec) || spec.kind === "pad_axis") send1(spec, false);
        held.delete(name);
      }
      return {};
    }
    if (mode === "hold") {
      const h = held.get(name);
      if (!h) {
        held.set(name, { spec, value, renewed: nowMs(), until: 0, rate: Boolean(args.rate) });
        if (!isAxis(spec) || spec.kind === "pad_axis") send1(spec, true, value);
      } else {
        if (spec.kind === "pad_axis" && h.value !== value) send1(spec, true, value);
        h.value = value;
        h.renewed = nowMs();
        h.rate = Boolean(args.rate);
      }
      const seconds = Number(args.seconds ?? 0);
      held.get(name).until = seconds > 0 ? gameTime() + seconds : 0;
      return {};
    }
    if (isAxis(spec)) {
      send1(spec, true, value);
      if (spec.kind === "pad_axis") send1(spec, false);
      return {};
    }
    return tap(spec, value, () => held.has(name));
  }

  // `input`: one of the game's actions (hooks.controls) by name, through its first key.
  function inputOp(args) {
    const action = String(args.action ?? "");
    const a = (hooks.controls?.() ?? []).find((c) => c.name === action);
    if (!a || !(a.keys ?? []).length) throw new OpError(`No input action named ${action}`);
    const name = unrealName(a.keys[0]);
    const spec = keySpec(name) ?? keySpec(a.keys[0]);
    if (!spec) throw new OpError(`The action ${action}'s key ${a.keys[0]} can't be pressed`);
    const mode = String(args.mode ?? "press");
    const key = `action:${action}`;
    if (mode === "release") {
      if (held.has(key)) send1(spec, false);
      held.delete(key);
      return {};
    }
    if (mode === "hold") {
      if (!held.has(key)) send1(spec, true);
      // (no lease, as Unreal's continuous injection: until released)
      held.set(key, { spec, value: 1, renewed: Infinity, until: 0 });
      return {};
    }
    return tap(spec, 1, () => held.has(key));
  }

  const keysForHooks = {
    hold(name, value = 1) {
      const spec = keySpec(name);
      if (!spec) throw new OpError(`Unknown key ${name}`);
      const h = held.get(name);
      if (!h || h.value !== value) send1(spec, true, value);
      held.set(name, { spec, value, renewed: Infinity, until: 0 });
    },
    release(name) {
      const h = held.get(name);
      if (h) send1(h.spec, false);
      held.delete(name);
    },
    frame: () => new Promise((r) => deferred.push(() => (r(), true))),
  };

  // `move`: walk the player in a kit direction for `seconds`. The game's move hook when it has one; else W/A/S/D
  // (moveKeys) held camera-relative (8 directions), as the Godot plugin's "keys". In 2D the keys are screen-relative:
  // kit X is right, kit Z is up.
  async function moveOp(args) {
    const p0 = playerNow();
    if (!p0) throw new OpError("No player", 404);
    const dir = frame.fromKitDir(args.direction ?? {});
    const scale = Math.max(-1, Math.min(1, Number(args.scale ?? 1)));
    const seconds = Number(args.seconds ?? 0.35);
    const start = [...p0.position];
    let via = "keys";
    if (hooks.move && args.via !== "keys") {
      via = "game";
      await hooks.move({ direction: dir.map((v) => v * scale), seconds, scale, keys: keysForHooks });
    } else {
      let fv;
      let rv;
      if (dimension === "2d") {
        const dl = Math.hypot(dir[0], dir[1]) || 1;
        rv = (dir[0] / dl) * scale;
        fv = (dir[1] / dl) * scale;
      } else {
        const cam = camera();
        const f = cam ? worldForward(cam) : [0, 0, -1];
        const fl = Math.hypot(f[0], f[2]) || 1;
        const fwd = [f[0] / fl, 0, f[2] / fl];
        const right = [-fwd[2], 0, fwd[0]];
        const dl = Math.hypot(dir[0], dir[2]) || 1;
        const want = [(dir[0] / dl) * scale, 0, (dir[2] / dl) * scale];
        fv = want[0] * fwd[0] + want[2] * fwd[2];
        rv = want[0] * right[0] + want[2] * right[2];
      }
      const keys = [[fv > 0.38, moveKeys.up], [fv < -0.38, moveKeys.down], [rv < -0.38, moveKeys.left], [rv > 0.38, moveKeys.right]].filter(([on]) => on).map(([, k]) => k);
      for (const k of keys) keysForHooks.hold(k);
      await new Promise((r) => setTimeout(r, seconds * 1000));
      for (const k of keys) keysForHooks.release(k);
    }
    const p1 = playerNow();
    const end = p1 ? p1.position : start;
    const dy = dimension === "2d" ? end[1] - start[1] : end[2] - start[2];
    return { moved: frame.r2(Math.hypot(end[0] - start[0], dy) * frame.scale), via };
  }

  function pointerOp(args) {
    const r = canvasRect();
    const w = Math.max(1, r.width);
    const h = Math.max(1, r.height);
    let sx;
    let sy;
    if (args.world || args.object) {
      const cam = camera();
      if (!cam) throw new OpError("No camera");
      let at;
      if (args.object) {
        const o = resolve(String(args.object));
        if (!o) throw new OpError(`Object ${args.object} does not exist`, 404);
        const box = worldBox(o);
        at = box ? box[0].map((v, i) => (v + box[1][i]) / 2) : worldPosition(o);
      } else at = frame.fromKit(args.world);
      const sp = project(cam, at);
      if (!sp || sp.behind) throw new OpError("That is behind the camera");
      sx = sp.x;
      sy = sp.y;
    } else if (args.screen_x !== undefined || args.screen_y !== undefined) {
      sx = Number(args.screen_x ?? 0);
      sy = Number(args.screen_y ?? 0);
    } else throw new OpError("pointer needs screen_x and screen_y, world or object");
    pointer = [((sx + 1) / 2) * w, ((1 - sy) / 2) * h];
    dispatchMouse("pointermove", { movementX: 0, movementY: 0 });
    dispatchMouse("mousemove", { movementX: 0, movementY: 0 });
    const round3 = (x) => Math.round(x * 1000) / 1000;
    return { screen_x: round3(sx), screen_y: round3(sy), x: Math.round(pointer[0]), y: Math.round(h - pointer[1]), on_screen: Math.abs(sx) <= 1 && Math.abs(sy) <= 1 };
  }

  // -- the UI: the DOM over the canvas -------------------------------------------------------------------------------

  function uiRoot() {
    return hooks.uiRoot ?? doc?.body ?? null;
  }

  function shown(el) {
    if (!el || !g.getComputedStyle) return Boolean(el);
    const r = el.getBoundingClientRect?.();
    if (r && (r.width === 0 || r.height === 0)) return false;
    let c = el;
    while (c && c.nodeType === 1) {
      const s = g.getComputedStyle(c);
      if (s.display === "none" || s.visibility === "hidden" || Number(s.opacity) === 0) return false;
      c = c.parentElement;
    }
    return true;
  }

  // An element's key: "<owner>.<id or class or tag>", owner the nearest ancestor with an id ("Screen" for none).
  function keyOf(el) {
    let owner = "Screen";
    for (let c = el.parentElement; c; c = c.parentElement)
      if (c.id) {
        owner = c.id;
        break;
      }
    const own = el.id || (typeof el.className === "string" && el.className.split(/\s+/)[0]) || el.tagName.toLowerCase();
    return `${owner}.${own}`;
  }

  function keyed(list) {
    const seen = new Map();
    return list.map((el) => {
      const k = keyOf(el);
      const n = (seen.get(k) ?? 0) + 1;
      seen.set(k, n);
      return [n === 1 ? k : `${k}#${n}`, el];
    });
  }

  function screenOp(args) {
    const root = uiRoot();
    const texts = {};
    if (root) {
      // the leaves that hold text: elements with a text node of their own
      const els = [...root.querySelectorAll("*")].filter((el) => el.tagName !== "SCRIPT" && el.tagName !== "STYLE" && [...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim()) && shown(el));
      for (const [k, el] of keyed(els)) texts[k] = el.textContent.trim();
    }
    const out = { texts };
    if (args.ui) out.ui = uiList();
    return out;
  }

  let lastUi = new Map();

  function uiList() {
    const root = uiRoot();
    lastUi = new Map();
    if (!root) return [];
    const els = [...root.querySelectorAll("button, input, select, textarea, a[href], [role=button], [onclick]")].filter(shown);
    return keyed(els).map(([id, el]) => {
      lastUi.set(id, el);
      const tag = el.tagName.toLowerCase();
      const type = (el.getAttribute?.("type") ?? "").toLowerCase();
      const kind = tag === "select" ? "combo" : type === "checkbox" || type === "radio" ? "checkbox" : type === "range" ? "slider" : tag === "textarea" || (tag === "input" && !["button", "submit"].includes(type)) ? "text_input" : "button";
      const r = el.getBoundingClientRect?.() ?? { left: 0, top: 0, width: 0, height: 0 };
      const item = { id, kind, name: el.id || el.getAttribute?.("name") || tag, owner: id.split(".")[0], label: (el.textContent || el.getAttribute?.("aria-label") || el.value || "").trim(), enabled: !el.disabled, path: id, class: tag, screen: { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) } };
      if (kind === "checkbox") item.checked = Boolean(el.checked);
      if (kind === "combo") {
        item.options = [...el.options].map((o) => o.text);
        item.selected = el.options[el.selectedIndex]?.text ?? "";
      }
      if (kind === "slider") Object.assign(item, { value: Number(el.value), min: Number(el.min), max: Number(el.max) });
      if (kind === "text_input") item.text = el.value;
      return item;
    });
  }

  function uiAct(args) {
    const id = String(args.id ?? "");
    if (!lastUi.has(id)) uiList();
    const el = lastUi.get(id);
    if (!el) throw new OpError(`no element ${id} on screen`);
    if (el.disabled) throw new OpError(`${id} is disabled`);
    const action = String(args.action ?? "click");
    if (action === "click") {
      el.click();
      return { id, via: "click" };
    }
    if (action === "set") {
      if (args.option !== undefined && el.tagName === "SELECT") {
        const i = [...el.options].findIndex((o) => o.text === String(args.option));
        if (i < 0) throw new OpError(`${id} has no option ${args.option}`);
        el.selectedIndex = i;
      } else if (el.type === "checkbox" || el.type === "radio") el.checked = Boolean(args.value);
      else el.value = String(args.value ?? args.text ?? "");
      el.dispatchEvent(new g.Event("input", { bubbles: true }));
      el.dispatchEvent(new g.Event("change", { bubbles: true }));
      return { id, via: "set" };
    }
    throw new OpError(`ui_act can't ${action}`);
  }

  // -- rc: reads and calls on the game's own objects -------------------------------------------------------------------

  // An object by path: a scene path (/Scene/Player), or a name in hooks.objects, with ".a.b" to go inside it.
  function rcResolve(path) {
    const s = String(path ?? "");
    const o = resolve(s);
    if (o) return o.rcTarget ?? o;
    const [head, ...rest] = s.split(".");
    let cur = hooks.objects?.[head];
    if (cur === undefined) return undefined;
    for (const k of rest) {
      if (cur === null || cur === undefined) return undefined;
      cur = cur[k];
    }
    return cur;
  }

  function plain(v, depth = 0) {
    if (v === null || v === undefined || typeof v === "number" || typeof v === "string" || typeof v === "boolean") return v ?? null;
    if (typeof v === "function") return "<function>";
    if (depth > 4) return "<...>";
    if (Array.isArray(v) || ArrayBuffer.isView(v)) return Array.from(v).slice(0, 200).map((x) => plain(x, depth + 1));
    if (v.isVector3) return { x: v.x, y: v.y, z: v.z };
    if (v.isObject3D) return pathOf(v) || `<${v.type}>`;
    const node = options.nodeOf?.(v);
    if (node) return pathOf(node) || `<${node.type}>`;
    if (v instanceof Map) return Object.fromEntries([...v].slice(0, 200).map(([k, x]) => [String(k), plain(x, depth + 1)]));
    if (v instanceof Set) return [...v].slice(0, 200).map((x) => plain(x, depth + 1));
    const out = {};
    for (const k of Object.keys(v).slice(0, 200)) {
      if (k === "parent" || k === "children") continue;
      try {
        out[k] = plain(v[k], depth + 1);
      } catch {
        out[k] = "<unreadable>";
      }
    }
    return out;
  }

  function rcOp(verb, body) {
    if (verb === "probe") return { verbs: ["property", "call", "describe"] };
    if (!body || typeof body !== "object") throw new OpError("rc needs a body");
    const obj = rcResolve(body.objectPath);
    if (obj === undefined) throw new OpError(`Object ${body.objectPath} does not exist`, 404);
    if (verb === "property") {
      const name = body.propertyName;
      if (body.access === "WRITE_ACCESS") {
        const value = body.propertyValue?.[name];
        if (obj === null || typeof obj !== "object") throw new OpError(`${body.objectPath} can't be written`);
        obj[name] = value;
        return {};
      }
      if (!name) return plain(obj);
      let v = obj;
      for (const part of String(name).split(/[.:]/)) {
        if (v === null || v === undefined) break;
        v = v[part];
      }
      if (v === undefined) throw new OpError(`${body.objectPath} has no property ${name}`, 404);
      return { [name]: plain(v) };
    }
    if (verb === "call") {
      const fn = obj?.[body.functionName];
      if (typeof fn !== "function") throw new OpError(`${body.objectPath} has no function ${body.functionName}`, 404);
      const params = body.parameters ?? {};
      const args = Array.isArray(params.args) ? params.args : Object.keys(params).length ? [params] : [];
      const r = fn.apply(obj, args);
      return Promise.resolve(r).then((v) => ({ ReturnValue: plain(v) }));
    }
    if (verb === "describe") {
      if (obj === null || typeof obj !== "object") return { Name: String(body.objectPath), Class: typeof obj, Properties: [], Functions: [] };
      const props = [];
      const fns = [];
      const seen = new Set();
      for (let p = obj; p && p !== Object.prototype; p = Object.getPrototypeOf(p))
        for (const k of Object.getOwnPropertyNames(p)) {
          if (seen.has(k) || k === "constructor" || k.startsWith("_")) continue;
          seen.add(k);
          let v;
          try {
            v = obj[k];
          } catch {
            continue;
          }
          (typeof v === "function" ? fns : props).push(typeof v === "function" ? { Name: k } : { Name: k, Type: Array.isArray(v) ? "array" : typeof v });
        }
      return { Name: String(body.objectPath), Class: obj.constructor?.name ?? "Object", Properties: props.slice(0, 300), Functions: fns.slice(0, 300) };
    }
    throw new OpError(`unknown rc verb ${verb}`);
  }

  // -- pictures: the canvas as JPEG ----------------------------------------------------------------------------------

  function clampDim(v, d) {
    const n = Number(v);
    return Number.isFinite(n) && n > 0 ? Math.max(64, Math.min(4096, Math.round(n))) : d;
  }

  function clampQuality(v, d) {
    const n = Number(v);
    return Number.isFinite(n) ? Math.max(10, Math.min(100, n)) : d;
  }

  // The canvas now, as a JPEG at most maxDim on its longer side: {jpeg: base64, width, height}, or null. The game's
  // render hook draws a frame first, so the WebGL buffer holds a picture to read (without preserveDrawingBuffer).
  function picture(maxDim, quality) {
    const c = pictureCanvas();
    if (!c || !doc?.createElement) return null;
    try {
      hooks.render?.();
      const w0 = c.width;
      const h0 = c.height;
      if (!w0 || !h0) return null;
      const k = Math.min(1, maxDim / Math.max(w0, h0));
      const w = Math.max(1, Math.round(w0 * k));
      const h = Math.max(1, Math.round(h0 * k));
      const out = doc.createElement("canvas");
      out.width = w;
      out.height = h;
      const ctx = out.getContext("2d");
      ctx.drawImage(c, 0, 0, w, h);
      const url = out.toDataURL("image/jpeg", quality / 100);
      const comma = url.indexOf(",");
      return { jpeg: url.slice(comma + 1), width: w, height: h };
    } catch (e) {
      return null;
    }
  }

  function startRecording(args) {
    if (!picture(64, 50)) throw new OpError("no picture: the canvas can't be read", 409);
    const f = Number(args.fps ?? 5);
    recording = { fps: Number.isFinite(f) && f > 0 ? Math.max(0.1, Math.min(60, f)) : 5, maxDim: clampDim(args.max_dim, 960), quality: clampQuality(args.quality, 75), n: 0, sent: 0, next: 0 };
    const c = pictureCanvas();
    const k = Math.min(1, recording.maxDim / Math.max(c.width, c.height));
    return { recording: true, width: Math.round(c.width * k), height: Math.round(c.height * k) };
  }

  function stopRecording() {
    const sent = recording ? recording.sent : 0;
    recording = null;
    return { recording: false, frames: sent };
  }

  // -- errors the game logs -------------------------------------------------------------------------------------------

  const onError = (e) => events.push({ kind: "log", t: t(), verbosity: "Error", category: "window", message: String(e?.message ?? e).slice(0, 2000) });
  const onRejection = (e) => events.push({ kind: "log", t: t(), verbosity: "Error", category: "promise", message: String(e?.reason?.message ?? e?.reason ?? e).slice(0, 2000) });
  g.addEventListener?.("error", onError);
  g.addEventListener?.("unhandledrejection", onRejection);

  raf(tick);

  return {
    connect,
    // An op answered here, without the socket (tests, the page's console): handle("pose", {})
    handle: (op, args = {}) => handle(op, args, 0),
    hello,
    tick,
    get state() {
      return state;
    },
    get session() {
      return session;
    },
    get pad() {
      return pad;
    },
    close() {
      closed = true;
      releaseAll();
      stopRecording();
      uninstallPad();
      g.removeEventListener?.("error", onError);
      g.removeEventListener?.("unhandledrejection", onRejection);
      if (ws) ws.close();
      state = "closed";
    },
  };
}

// What `view` doesn't tell here, as the Godot plugin's `omitted`.
const OMITTED = ["held", "openings", "ways_up", "lanes and slopes", "under/overhead", "look", "blocks_player", "sonar", "line of sight"];
