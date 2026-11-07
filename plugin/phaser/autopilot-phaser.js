// Autopilot for Phaser games (docs: plugin/phaser/README.md).
//
// Phaser has no Three.js scene graph, so this module builds one of the shape Autopilot's browser bridge
// (plugin/threejs/autopilot-bridge.js) reads: objects with `name`, `type`, `children`, `parent`, `visible`,
// `matrixWorld.elements` and `geometry.boundingBox`, and a camera with `matrixWorldInverse` and `projectionMatrix`.
// phaserBridgeOptions(game, options) returns the options for the bridge's connectBridge(): the bridge then answers the
// engine-neutral ops for the Phaser game in 2D (the bridge's `dimension: "2d"`).
//
// The 2D frame is the Godot plugin's: kit X is screen right, kit Z is screen up, kit Y is 0, and 1 pixel is 1 kit
// unit. Phaser's world is x right, y down, in pixels; the scene this module builds is x right, y up (y = -Phaser y),
// z = 0, so the bridge's 2D frame gives kit (X, Y, Z) = (x, 0, -y).
//
// The module imports nothing (not even phaser): it reads the fields every Phaser 3.60+ and 4 game object has (x, y,
// rotation, scaleX, scaleY, width, height, originX, originY, scrollFactorX/Y, parentContainer, list) and the
// cameras' worldView. A Node test drives it with plain objects (plugin/phaser/test/).
//
// Licensed under the Apache License 2.0, as the rest of Autopilot.

export const VERSION = "0.1.0";
// Where the 2D camera stands in front of the plane z = 0 (scene units). Any positive number works: the projection is
// orthographic, and the bridge measures 2D distances on the plane.
const CAMERA_Z = 1000;
const IDENTITY = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1];

// -- Phaser's 2D transforms ----------------------------------------------------------------------------------------

// A game object's world transform, with its parent Containers': {a, b, c, d, tx, ty}, as Phaser's TransformMatrix
// (x' = a x + c y + tx, y' = b x + d y + ty), in Phaser's axes (y down). It leaves out the display origin, as
// getWorldTransformMatrix does.
export function affineOf(go) {
  const local = (o) => {
    const r = Number(o.rotation ?? 0);
    const sx = Number(o.scaleX ?? 1);
    const sy = Number(o.scaleY ?? 1);
    const cos = Math.cos(r);
    const sin = Math.sin(r);
    return { a: cos * sx, b: sin * sx, c: -sin * sy, d: cos * sy, tx: Number(o.x ?? 0), ty: Number(o.y ?? 0) };
  };
  let m = local(go);
  for (let p = go.parentContainer; p; p = p.parentContainer) {
    const q = local(p);
    m = {
      a: q.a * m.a + q.c * m.b, b: q.b * m.a + q.d * m.b,
      c: q.a * m.c + q.c * m.d, d: q.b * m.c + q.d * m.d,
      tx: q.a * m.tx + q.c * m.ty + q.tx, ty: q.b * m.tx + q.d * m.ty + q.ty,
    };
  }
  return m;
}

// A Phaser affine as the column-major 4x4 of the scene this module builds (y up): a local point (x, y) in that scene
// (y up) is Phaser's local (x, -y); its world point is (Phaser x', -Phaser y').
export function nativeMatrix(m) {
  return [m.a, -m.b, 0, 0, -m.c, m.d, 0, 0, 0, 0, 1, 0, m.tx, -m.ty, 0, 1];
}

// A Phaser point (y down) in the scene (y up).
export function toNative(x, y) {
  return [Number(x), -Number(y), 0];
}

// The local box of a game object with a size, in the scene's local axes (y up), from its origin: {min, max}, or null.
export function localBox(go) {
  const w = Number(go.width ?? 0);
  const h = Number(go.height ?? 0);
  if (!(w > 0 && h > 0)) return null;
  const ox = Number(go.originX ?? 0.5);
  const oy = Number(go.originY ?? 0.5);
  // (0 - v: never -0)
  return { min: { x: 0 - ox * w, y: 0 - (1 - oy) * h, z: 0 }, max: { x: (1 - ox) * w, y: oy * h, z: 0 } };
}

// An orthographic camera of the shape the bridge reads, for a Phaser camera's world view (the world rectangle it
// shows): it looks down -z at the plane, its middle on the view's middle.
export function cameraOf(cam) {
  const wv = cam?.worldView ?? { x: 0, y: 0, width: Number(cam?.width ?? 0), height: Number(cam?.height ?? 0) };
  const w = Math.max(1e-6, Number(wv.width));
  const h = Math.max(1e-6, Number(wv.height));
  const cx = Number(wv.x) + w / 2;
  const cy = -(Number(wv.y) + h / 2);
  const near = 0.1;
  const far = 2 * CAMERA_Z;
  return {
    name: String(cam?.name || "Camera"),
    type: "OrthographicCamera",
    isCamera: true,
    children: [],
    visible: true,
    userData: {},
    matrixWorld: { elements: [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, cx, cy, CAMERA_Z, 1] },
    matrixWorldInverse: { elements: [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, -cx, -cy, -CAMERA_Z, 1] },
    projectionMatrix: { elements: [2 / w, 0, 0, 0, 0, 2 / h, 0, 0, 0, 0, -2 / (far - near), 0, 0, 0, -(far + near) / (far - near), 1] },
    viewWidth: w,
    viewHeight: h,
    rcTarget: cam,
  };
}

// -- the options for connectBridge ---------------------------------------------------------------------------------

// phaserBridgeOptions(game, options) -> the options for connectBridge (plugin/threejs/autopilot-bridge.js). game: the
// Phaser.Game. options: every option of connectBridge (url, project, allowRemote, moveKeys...), and:
//   phaser: the Phaser module (its VERSION goes into hello; else globalThis.Phaser);
//   name: the root object's name ("Game"): things are /Game/<scene key>/<object>;
//   scenes: the scenes to read: an array of keys or a function (scene) -> bool (every active scene by default);
//   cameraScene: the key of the scene whose main camera is the view (the first scene read by default);
//   hud: false leaves out objects drawn in screen space (scroll factor 0, or in a scene other than cameraScene);
//        they are in by default, placed where they show over the world (userData.hud);
//   include(gameObject) -> bool: false leaves an object (and what it holds) out;
//   mirror: false never copies the frames (the bridge then reads the canvas as it is);
//   hooks: connectBridge's hooks, with Phaser's axes (pixels, y down) where they give positions:
//     player() -> {object: the player's game object, x?, y? (its feet; the bottom middle of its box by default),
//                  velocity?: {x, y} | [x, y] (pixels a second; its Arcade body's by default), onGround? (its body's
//                  blocked.down or touching.down by default), facing?: 1 | -1 (-1 when flipX by default),
//                  halfHeight?, state?} | null;
//     move({direction: [x, y] (y down), seconds, scale, keys}): walk the player that way the game's own way;
//     restart({map}), levels(), pause(paused), paused(), gameTime(): by default the camera scene's own (restart()
//     and scene.start(map), the game's scene keys, game.pause() and game.resume(), the scene's clock);
//     the others (movement, controls, render, objects, classOf, uiRoot) as connectBridge has them (classOf gets a
//     scene object: its `gameObject` is the Phaser game object).
// A game object's Data Manager gives what Three.js keeps in userData: "class", "thing" and "label" (a Text's label is
// its text).
export function phaserBridgeOptions(game, options = {}) {
  const g = options.global ?? globalThis;
  const userHooks = options.hooks ?? {};
  const nodes = new WeakMap(); // game object or scene -> its node, kept so a node is the same object from frame to frame
  const root = { name: String(options.name ?? "Game"), type: "Game", children: [], parent: null, visible: true, userData: {}, matrixWorld: { elements: IDENTITY }, rcTarget: game };
  let fresh = false; // the scene matches the game: true until the current task ends
  let camNode = null;

  const scenesRead = () => {
    const all = game?.scene?.getScenes ? game.scene.getScenes(true) : [];
    const want = options.scenes;
    if (Array.isArray(want)) return all.filter((s) => want.includes(keyOf(s)));
    if (typeof want === "function") return all.filter((s) => want(s));
    return all;
  };
  const worldScene = () => {
    const read = scenesRead();
    if (options.cameraScene) {
      const s = game?.scene?.getScene?.(options.cameraScene) ?? read.find((x) => keyOf(x) === options.cameraScene);
      if (s) return s;
    }
    return read[0] ?? null;
  };
  const mainCamera = () => worldScene()?.cameras?.main ?? null;

  function nodeFor(key, make) {
    let n = nodes.get(key);
    if (!n) {
      n = make();
      nodes.set(key, n);
    }
    return n;
  }

  // Rebuild the scene at most once a task: an op reads it many times, and the next op (even while the game is paused)
  // sees what changed since.
  function refresh() {
    if (fresh) return;
    fresh = true;
    queueMicrotask(() => (fresh = false));
    const cam = mainCamera();
    camNode = cam ? cameraOf(cam) : null;
    const camWorld = cam?.worldView ?? { x: 0, y: 0 };
    const zoom = Number(cam?.zoom ?? 1) || 1;
    const camScene = worldScene();
    root.children = [];
    for (const scene of scenesRead()) {
      const sn = nodeFor(scene, () => ({ type: "Scene", children: [], userData: {}, matrixWorld: { elements: IDENTITY }, rcTarget: scene }));
      sn.name = keyOf(scene);
      sn.parent = root;
      sn.visible = scene.sys?.settings?.visible !== false;
      sn.children = [];
      root.children.push(sn);
      const own = scene.cameras?.main;
      const list = scene.children?.list ?? scene.sys?.displayList?.list ?? [];
      // A screen point (the canvas' pixels) as the world point under it in the camera scene
      const toWorld = (sx, sy) => [Number(camWorld.x) + sx / zoom, Number(camWorld.y) + sy / zoom];
      const place = (go, m) => {
        const sf0 = Number(go.scrollFactorX ?? 1) === 0 && Number(go.scrollFactorY ?? 1) === 0;
        const other = scene !== camScene;
        if (!sf0 && !other) return { m, hud: false };
        // Screen space: where it shows on the canvas, then the world under that (a translation; zoom and rotation of
        // the cameras left out)
        let sx = m.tx;
        let sy = m.ty;
        if (!sf0 && own?.worldView) {
          const z = Number(own.zoom ?? 1) || 1;
          sx = (m.tx - Number(own.worldView.x)) * z;
          sy = (m.ty - Number(own.worldView.y)) * z;
        }
        const [wx, wy] = toWorld(sx, sy);
        return { m: { ...m, tx: wx, ty: wy }, hud: true };
      };
      const add = (parent, go) => {
        if (!go || (options.include && !options.include(go))) return;
        const n = nodeFor(go, () => ({ children: [], userData: {}, gameObject: go, rcTarget: go, matrixWorld: { elements: IDENTITY } }));
        const placed = place(go, affineOf(go));
        if (placed.hud && options.hud === false) return;
        n.name = String(go.name ?? "");
        n.type = String(go.type || go.constructor?.name || "GameObject");
        n.parent = parent;
        n.visible = go.visible !== false;
        n.matrixWorld = { elements: nativeMatrix(placed.m) };
        const box = localBox(go);
        n.geometry = box ? { boundingBox: box } : undefined;
        n.userData = userDataOf(go, placed.hud);
        n.children = [];
        parent.children.push(n);
        for (const c of go.list ?? []) add(n, c);
      };
      for (const go of list) add(sn, go);
    }
  }

  const nodeOf = (v) => {
    if (!v || typeof v !== "object") return null;
    if (v === game) return root;
    refresh();
    return nodes.get(v) ?? null;
  };

  // The frames: a WebGL canvas made without preserveDrawingBuffer reads back blank once the browser shows it, so a
  // copy of each frame is kept, made on postrender while the buffer still holds it. Checked each frame: the renderer
  // may not exist yet when the game calls this. null: no frame copied, the canvas is read as it is.
  const doc = options.document ?? g.document;
  const WEBGL = 2; // Phaser.WEBGL
  let mirror = null;
  if (options.mirror !== false && doc?.createElement) {
    game?.events?.on?.("postrender", () => {
      const c = game.canvas;
      if (game.renderer?.type !== WEBGL || game.config?.preserveDrawingBuffer || !c?.width || !c?.height) return;
      mirror ??= doc.createElement("canvas");
      if (mirror.width !== c.width) mirror.width = c.width;
      if (mirror.height !== c.height) mirror.height = c.height;
      mirror.getContext("2d")?.drawImage(c, 0, 0);
    });
  }

  const hooks = { ...userHooks };
  hooks.player = () => playerOf(userHooks.player?.());
  if (userHooks.move) {
    hooks.move = (a) => userHooks.move({ ...a, direction: [a.direction[0], -a.direction[1]] });
  }
  if (!hooks.restart) {
    hooks.restart = ({ map } = {}) => {
      const s = worldScene();
      if (!s?.scene) throw new Error("no scene to restart");
      if (map) s.scene.start(map);
      else s.scene.restart();
      return afterFrames(2);
    };
  }
  if (!hooks.levels) {
    hooks.levels = () => ({ levels: (game?.scene?.scenes ?? []).map(keyOf), playing: keyOf(worldScene()) });
  }
  if (!hooks.pause && typeof game?.pause === "function") {
    hooks.pause = (p) => {
      if (p) game.pause();
      else game.resume?.();
      return Boolean(game.isPaused);
    };
    hooks.paused = hooks.paused ?? (() => Boolean(game.isPaused));
  }
  if (!hooks.gameTime) {
    hooks.gameTime = () => Number(worldScene()?.time?.now ?? game?.loop?.time ?? 0) / 1000;
  }

  // The player hook's answer in the bridge's shape (the scene's axes, y up). Each field the hook leaves out comes from
  // the game object: its feet the bottom middle of its box (its position when it has no box).
  function playerOf(p) {
    if (!p) return null;
    const go = p.object;
    const node = go ? nodeOf(go) : null;
    const box = node ? boxOf(node) : null;
    const x = p.x ?? (box ? (box[0][0] + box[1][0]) / 2 : go?.x);
    const y = p.y ?? (box ? -box[0][1] : go?.y);
    if (!Number.isFinite(Number(x)) || !Number.isFinite(Number(y))) return null;
    const feet = toNative(x, y);
    const half = p.halfHeight ?? (box ? (box[1][1] - box[0][1]) / 2 : 0);
    const body = go?.body;
    const v = p.velocity ?? body?.velocity;
    const vel = v ? [Number(v.x ?? v[0] ?? 0), -Number(v.y ?? v[1] ?? 0), 0] : [0, 0, 0];
    const onGround = p.onGround ?? (body ? Boolean(body.blocked?.down || body.touching?.down) : undefined);
    const facing = p.facing ?? (go?.flipX ? -1 : 1);
    return {
      object: node ?? undefined, position: feet, velocity: vel, forward: [facing < 0 ? -1 : 1, 0, 0],
      ...(onGround !== undefined ? { onGround } : {}), halfHeight: Number(half),
      ...(p.state !== undefined ? { state: p.state } : {}),
    };
  }

  // Resolves after n frames are drawn (Phaser starts a scene on its next step), or after a second: a paused game draws
  // no frame, and a restart must not wait for it forever.
  function afterFrames(n) {
    return new Promise((resolve) => {
      let left = n;
      const done = () => {
        game.events?.off?.("postrender", step);
        clearTimeout(timer);
        resolve();
      };
      const step = () => {
        left -= 1;
        if (left <= 0) done();
      };
      const timer = setTimeout(done, 1000);
      game.events?.on?.("postrender", step);
    });
  }

  return {
    ...options,
    dimension: "2d",
    engine: "phaser",
    engineVersion: String(options.engineVersion ?? options.phaser?.VERSION ?? g.Phaser?.VERSION ?? ""),
    thingDepth: 3,
    scene: () => {
      refresh();
      return root;
    },
    camera: () => {
      refresh();
      return camNode;
    },
    canvas: options.canvas ?? (() => game?.canvas ?? null),
    pictureCanvas: options.pictureCanvas ?? (() => mirror ?? game?.canvas ?? null),
    nodeOf,
    hooks,
  };
}

function keyOf(scene) {
  return String(scene?.sys?.settings?.key ?? scene?.scene?.key ?? scene?.key ?? "");
}

function userDataOf(go, hud) {
  const data = (k) => (typeof go.getData === "function" ? go.getData(k) : undefined);
  const out = {};
  const cls = data("class");
  if (cls !== undefined && cls !== null) out.class = String(cls);
  if (data("thing") === true) out.thing = true;
  const label = data("label") ?? (typeof go.text === "string" ? go.text : undefined);
  if (label !== undefined && label !== null && String(label).trim()) out.label = String(label).slice(0, 120);
  if (hud) out.hud = true;
  return out;
}

// The world box [min, max] of a node and its children (the scene's axes), or null.
function boxOf(node) {
  let min = [Infinity, Infinity, Infinity];
  let max = [-Infinity, -Infinity, -Infinity];
  let have = false;
  const stack = [node];
  while (stack.length) {
    const n = stack.pop();
    if (n.visible === false) continue;
    const b = n.geometry?.boundingBox;
    if (b) {
      const m = n.matrixWorld.elements;
      for (const x of [b.min.x, b.max.x])
        for (const y of [b.min.y, b.max.y]) {
          const p = [m[0] * x + m[4] * y + m[12], m[1] * x + m[5] * y + m[13], 0];
          for (let i = 0; i < 3; i += 1) {
            if (p[i] < min[i]) min[i] = p[i];
            if (p[i] > max[i]) max[i] = p[i];
          }
        }
      have = true;
    }
    for (const k of n.children ?? []) stack.push(k);
  }
  return have ? [min, max] : null;
}
