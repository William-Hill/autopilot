// The bridge's ops on a stand-in game (fake_game.mjs), without a socket: `node --test plugin/threejs/test/*.test.mjs`.

import { test } from "node:test";
import assert from "node:assert/strict";
import { createBridge, isLoopbackUrl, keySpec, makeFrame, project, unrealName, worldBox } from "../autopilot-bridge.js";
import { add, camera, makeFakeGame, object } from "./fake_game.mjs";

test("the kit frame is Godot's: kit = 100 * (-z, x, y)", () => {
  const f = makeFrame(1);
  assert.deepEqual(f.kit([1, 2, -3]), { X: 300, Y: 100, Z: 200 });
  assert.deepEqual(f.fromKit({ X: 300, Y: 100, Z: 200 }), [1, 2, -3]);
  assert.equal(f.yawOf([0, 0, -1]), 0); // forward
  assert.equal(f.yawOf([1, 0, 0]), 90); // right: yaw turns right
  assert.equal(f.pitchOf([0, 1, -1]), 45);
  assert.deepEqual(makeFrame(0.01).kit([1, 0, 0]), { X: 0, Y: 1, Z: 0 }); // a game in centimetres
});

test("key names are Unreal's, as the kit presses them", () => {
  assert.deepEqual(keySpec("W"), { kind: "key", code: "KeyW" });
  assert.deepEqual(keySpec("SpaceBar"), { kind: "key", code: "Space" });
  assert.deepEqual(keySpec("LeftShift"), { kind: "key", code: "ShiftLeft" });
  assert.deepEqual(keySpec("One"), { kind: "key", code: "Digit1" });
  assert.deepEqual(keySpec("Gamepad_FaceButton_Bottom"), { kind: "pad_button", button: 0 });
  assert.deepEqual(keySpec("Gamepad_LeftY"), { kind: "pad_axis", axis: 1, sign: -1 });
  assert.deepEqual(keySpec("KeyF"), { kind: "key", code: "KeyF" });
  assert.equal(keySpec(""), null);
  assert.equal(keySpec("NoSuchKey"), null);
  assert.equal(unrealName("KeyW"), "W");
  assert.equal(unrealName("Space"), "SpaceBar");
  assert.equal(unrealName("ShiftLeft"), "LeftShift");
  assert.equal(unrealName("ArrowUp"), "Up");
});

test("project and worldBox work on Three.js' plain arrays", () => {
  const cam = camera([0, 0, 10]);
  const p = project(cam, [0, 0, 0]);
  assert.ok(p.inView && Math.abs(p.x) < 1e-9 && Math.abs(p.y) < 1e-9);
  assert.ok(project(cam, [0, 0, 20]).behind);
  const box = worldBox(object("B", "Mesh", [1, 2, 3], [0.5, 1, 1.5]));
  assert.deepEqual(box, [[0.5, 1, 1.5], [1.5, 3, 4.5]]);
});

test("hello says engine threejs and never shares runs' data", () => {
  const { bridge } = makeFakeGame();
  const h = bridge.hello();
  assert.equal(h.type, "hello");
  assert.equal(h.protocol, 1);
  assert.equal(h.engine, "threejs");
  assert.equal(h.project, "Fake Rink");
  assert.equal(h.share, false);
  assert.ok(h.features.includes("timed_hold") && h.features.includes("pointer"));
});

test("find, transforms, pose and movement answer in the kit frame", async () => {
  const { bridge } = makeFakeGame();
  const all = (await bridge.handle("find", {})).objects;
  assert.ok(all.includes("/Scene/Rail") && all.includes("/Scene/Rail#2") && all.includes("/Scene/Group/Cone"));
  assert.deepEqual((await bridge.handle("find", { class: "Rail" })).objects, ["/Scene/Rail", "/Scene/Rail#2"]);
  assert.deepEqual((await bridge.handle("find", { name: "cone" })).objects, ["/Scene/Group/Cone"]);
  const things = (await bridge.handle("find", { things: true })).objects;
  assert.ok(things.includes("/Scene/Group/Cone") && things.includes("/Scene/Player") && !things.includes("/Scene/Player/Body"));
  await assert.rejects(bridge.handle("find", { class: "Dragon" }), /Unknown class Dragon/);

  const t = (await bridge.handle("transforms", { objects: ["/Scene/Rail", "/Scene/Nope"] })).objects;
  assert.deepEqual(t["/Scene/Rail"].location, { X: 0, Y: 200, Z: 50 });
  assert.deepEqual(t["/Scene/Rail"].extent, { X: 300, Y: 5, Z: 5 });
  assert.equal(t["/Scene/Rail"].active, true);
  assert.equal(t["/Scene/Nope"], null);

  const pose = await bridge.handle("pose", {});
  assert.equal(pose.path, "/Scene/Player");
  assert.deepEqual(pose.feet, { X: -500, Y: 0, Z: 0 });
  assert.deepEqual(pose.at, { X: -500, Y: 0, Z: 90 });
  assert.deepEqual(pose.eye, { X: -1000, Y: 0, Z: 200 });
  assert.equal(pose.yaw, 0);
  assert.equal(pose.on_ground, true);

  const m = await bridge.handle("movement", {});
  assert.equal(m.walk_speed, 400);
  assert.equal(m.gravity, 980);
  assert.equal(m.max_slope, 40);
  assert.equal(m.step, null);
});

test("view lists the things in the camera's view, nearest first, never the player", async () => {
  const { bridge } = makeFakeGame();
  const v = await bridge.handle("view", {});
  const names = v.visible.map((i) => i.name);
  assert.ok(names.includes("Cone") && names.includes("Rail"));
  assert.ok(!names.includes("Player") && !names.includes("Body"));
  assert.ok(!names.includes("Ground")); // (a wide flat floor is left out, as the other plugins do)
  const d = v.visible.map((i) => i.distance);
  assert.deepEqual(d, [...d].sort((a, b) => a - b));
  assert.equal(v.camera.yaw, 0);
  assert.ok(Math.abs(v.camera.vfov - 60) < 0.01);
  const only = await bridge.handle("view", { only: ["Cone"] });
  assert.deepEqual(only.visible.map((i) => i.name), ["Cone"]);
});

test("keys: a press goes down and up a couple of frames later; a hold stays until its lease runs out", async () => {
  const game = makeFakeGame();
  const { bridge, keyLog, pump } = game;
  const pressed = bridge.handle("key", { key: "SpaceBar", mode: "press" });
  assert.deepEqual(keyLog, ["keydown:Space"]);
  pump(5);
  await pressed;
  assert.deepEqual(keyLog, ["keydown:Space", "keyup:Space"]);

  await bridge.handle("key", { key: "W", mode: "hold", lease: 0.5 });
  pump(10); // 160 ms: still held
  assert.ok(game.down.has("KeyW"));
  assert.ok(game.state.position[2] < 5);
  pump(30); // past the lease: let go
  assert.ok(!game.down.has("KeyW"));
  await assert.rejects(bridge.handle("key", { key: "NoSuchKey" }), /Unknown key/);
});

test("a timed hold lets go after that much game time", async () => {
  const game = makeFakeGame();
  await game.bridge.handle("key", { key: "W", mode: "hold", seconds: 0.1, lease: 10 });
  game.pump(3);
  assert.ok(game.down.has("KeyW"));
  game.pump(10);
  assert.ok(!game.down.has("KeyW"));
});

test("gamepad keys go to a virtual standard gamepad that getGamepads lists", async () => {
  const game = makeFakeGame();
  await game.bridge.handle("key", { key: "Gamepad_FaceButton_Bottom", mode: "hold" });
  const pads = game.nav.getGamepads();
  assert.equal(pads[0].mapping, "standard");
  assert.equal(pads[0].buttons[0].pressed, true);
  await game.bridge.handle("key", { key: "Gamepad_LeftX", mode: "hold", value: -1 });
  assert.equal(pads[0].axes[0], -1);
  await game.bridge.handle("key", { mode: "release_all" });
  assert.equal(game.nav.getGamepads().length, 0); // (idle: listed no more)
});

test("input presses an action of the game by its first key", async () => {
  const game = makeFakeGame();
  await game.bridge.handle("input", { action: "push", mode: "hold" });
  assert.ok(game.down.has("KeyW"));
  await game.bridge.handle("input", { action: "push", mode: "release" });
  assert.ok(!game.down.has("KeyW"));
  await assert.rejects(game.bridge.handle("input", { action: "fly" }), /No input action named fly/);
});

test("move holds the camera-relative keys and says how far the player went", async () => {
  const game = makeFakeGame();
  const moving = game.bridge.handle("move", { direction: { X: 1, Y: 0, Z: 0 }, seconds: 0.2 });
  const timer = setInterval(() => game.pump(1), 16);
  const r = await moving;
  clearInterval(timer);
  assert.equal(r.via, "keys");
  assert.ok(r.moved > 0, JSON.stringify(r));
  assert.ok(game.keyLog.includes("keydown:KeyW") && game.keyLog.includes("keyup:KeyW"));
});

test("session restart calls the game's restart hook; context names the player and camera", async () => {
  const game = makeFakeGame();
  game.state.position = [3, 0, 3];
  const r = await game.bridge.handle("session", { action: "restart" });
  assert.equal(r.running, true);
  assert.equal(game.state.restarts, 1);
  assert.deepEqual(game.state.position, [0, 0, 5]);
  const c = await game.bridge.handle("context", {});
  assert.equal(c.engine, "threejs");
  assert.equal(c.character, "/Scene/Player");
  assert.equal(c.camera, "/Scene/Camera");
  assert.deepEqual(c.controls.map((a) => [a.name, a.keys]), [["push", ["W", "Up"]], ["jump", ["SpaceBar"]]]);
});

test("rc reads, writes and calls the game's own objects", async () => {
  const game = makeFakeGame();
  assert.deepEqual(await game.bridge.handle("rc", { verb: "property", body: { objectPath: "state", propertyName: "score" } }), { score: 7 });
  await game.bridge.handle("rc", { verb: "call", body: { objectPath: "game", functionName: "addPoints", parameters: { points: 5 } } });
  assert.equal(game.state.score, 12);
  await game.bridge.handle("rc", { verb: "property", body: { objectPath: "state", propertyName: "score", propertyValue: { score: 1 }, access: "WRITE_ACCESS" } });
  assert.equal(game.state.score, 1);
  const d = await game.bridge.handle("rc", { verb: "describe", body: { objectPath: "state" } });
  assert.ok(d.Properties.some((p) => p.Name === "score"));
  await assert.rejects(game.bridge.handle("rc", { verb: "property", body: { objectPath: "nothing" } }), /does not exist/);
});

test("an op the bridge doesn't have says unknown op (the brain checks for it)", async () => {
  const game = makeFakeGame();
  await assert.rejects(game.bridge.handle("codex_manifest", {}), /unknown op codex_manifest/);
  await assert.rejects(game.bridge.handle("capture", {}), /no picture/);
});

test("watch notes what moved", async () => {
  const game = makeFakeGame();
  game.down.add("KeyW");
  const w = game.bridge.handle("watch", { objects: ["/Scene/Player", "/Scene/Rail"], seconds: 0.05 });
  for (let i = 0; i < 10; i += 1) {
    game.pump(1);
    await new Promise((r) => setTimeout(r, 0));
  }
  const r = await w;
  assert.deepEqual(r.changes.map((c) => [c.object, c.what]), [["/Scene/Player", "moved"]]);
});

test("the bridge connects only to this machine unless the game allows another host", () => {
  for (const ok of ["ws://127.0.0.1:8765/v1/bridge", "ws://localhost:8765/v1/bridge", "ws://[::1]:8765/v1/bridge", "wss://127.0.0.2/x"])
    assert.equal(isLoopbackUrl(ok), true, ok);
  for (const bad of ["ws://example.com/v1/bridge", "wss://127.0.0.1.example.com/", "http://127.0.0.1:8765/", "ws://10.0.0.5/", "not a url", ""])
    assert.equal(isLoopbackUrl(bad), false, bad);
  const opened = [];
  class FakeWS {
    constructor(u) {
      opened.push(u);
    }
  }
  const quiet = { global: { addEventListener() {}, removeEventListener() {} }, requestFrame: () => {}, WebSocket: FakeWS };
  const refused = createBridge({ ...quiet, url: "wss://example.com/v1/bridge" });
  refused.connect();
  assert.equal(refused.state, "refused");
  assert.deepEqual(opened, []);
  const allowed = createBridge({ ...quiet, url: "wss://example.com/v1/bridge", allowRemote: true });
  allowed.connect();
  assert.deepEqual(opened, ["wss://example.com/v1/bridge"]);
  const local = createBridge({ ...quiet });
  local.connect();
  assert.equal(opened.at(-1), "ws://127.0.0.1:8765/v1/bridge");
});

// -- the review findings of William-Hill/atl-arcade#386 --------------------------------------------------------------

test("rc follows own properties only, calls own functions only, and writes only what the game exports", async () => {
  const game = makeFakeGame();
  const rc = (verb, body) => game.bridge.handle("rc", { verb, body });
  // What the game registers still works
  assert.deepEqual(await rc("property", { objectPath: "state", propertyName: "score" }), { score: 7 });
  assert.deepEqual(await rc("call", { objectPath: "game", functionName: "addPoints", parameters: { points: 1 } }), { ReturnValue: 8 });
  // A path through an inherited or a banned name fails
  await assert.rejects(rc("property", { objectPath: "game.addPoints", propertyName: "constructor" }), /does not follow constructor/);
  await assert.rejects(rc("property", { objectPath: "game.addPoints.constructor" }), /does not follow constructor/);
  await assert.rejects(rc("property", { objectPath: "state", propertyName: "__proto__" }), /does not follow __proto__/);
  await assert.rejects(rc("property", { objectPath: "game.addPoints", propertyName: "prototype" }), /does not follow prototype/);
  await assert.rejects(rc("property", { objectPath: "constructor" }), /does not follow constructor/);
  await assert.rejects(rc("property", { objectPath: "toString" }), /inherited/);
  await assert.rejects(rc("property", { objectPath: "state", propertyName: "hasOwnProperty" }), /inherited/);
  await assert.rejects(rc("call", { objectPath: "state", functionName: "toString" }), /inherited/);
  await assert.rejects(rc("call", { objectPath: "game", functionName: "constructor", parameters: { args: ["return 1"] } }), /does not follow constructor/);
  await assert.rejects(rc("call", { objectPath: "game.addPoints", functionName: "call" }), /inherited/);
  // A method on a class' prototype is inherited: out of reach
  class Counter {
    constructor() {
      this.n = 0;
    }
    bump() {
      this.n += 1;
    }
  }
  const counter = new Counter();
  const g2 = makeFakeGame();
  g2.hooks.objects.counter = counter;
  await assert.rejects(g2.bridge.handle("rc", { verb: "call", body: { objectPath: "counter", functionName: "bump" } }), /inherited/);
  assert.equal(counter.n, 0);
  const d = await g2.bridge.handle("rc", { verb: "describe", body: { objectPath: "counter" } });
  assert.deepEqual(d.Properties.map((p) => p.Name), ["n"]);
  assert.deepEqual(d.Functions, []);
  assert.equal(d.Class, "Counter");
  // A write needs the game's export (rcWritable)
  await assert.rejects(rc("property", { objectPath: "state", propertyName: "speed", propertyValue: { speed: 99 }, access: "WRITE_ACCESS" }), /not writable/);
  assert.equal(game.state.speed, 0);
  await assert.rejects(rc("property", { objectPath: "game", propertyName: "addPoints", propertyValue: { addPoints: 1 }, access: "WRITE_ACCESS" }), /not writable/);
  assert.equal(typeof game.hooks.objects.game.addPoints, "function");
  await rc("property", { objectPath: "state", propertyName: "score", propertyValue: { score: 3 }, access: "WRITE_ACCESS" });
  assert.equal(game.state.score, 3);
});

// A fake WebSocket the test opens, feeds and closes by hand.
function fakeSockets() {
  const sockets = [];
  class FakeWS {
    constructor(url) {
      this.url = url;
      this.readyState = 0;
      this.sent = [];
      sockets.push(this);
    }
    send(s) {
      this.sent.push(JSON.parse(s));
    }
    close() {
      this.readyState = 3;
      this.onclose?.();
    }
    open() {
      this.readyState = 1;
      this.onopen?.();
      this.onmessage?.({ data: JSON.stringify({ type: "welcome", session: "s1" }) });
    }
    request(id, op, args) {
      this.onmessage?.({ data: JSON.stringify({ type: "request", id, op, args }) });
    }
    responses() {
      return this.sent.filter((f) => f.type === "response");
    }
  }
  return { sockets, WebSocket: FakeWS };
}

const settle = () => new Promise((r) => setTimeout(r, 0));

test("an answer goes only to the socket that sent the request", async () => {
  const { sockets, WebSocket } = fakeSockets();
  const game = makeFakeGame({ WebSocket });
  game.bridge.connect();
  const a = sockets[0];
  a.open();
  a.request(1, "watch", { objects: ["/Scene/Player"], seconds: 0.05 });
  a.close(); // the agent leaves while its watch runs
  game.bridge.connect(); // the next agent
  const b = sockets[1];
  b.open();
  b.request(2, "ping", {});
  for (let i = 0; i < 10; i += 1) {
    game.pump(1);
    await settle();
  }
  assert.deepEqual(b.responses().map((r) => r.id), [2]); // never A's watch
  assert.deepEqual(a.responses(), []);
  // A late close of the old socket changes nothing on the new one
  a.onclose();
  assert.equal(game.bridge.state, "connected");
  game.bridge.close();
});

test("release_all and close() let go of a tap in flight", async () => {
  const game = makeFakeGame();
  const pressed = game.bridge.handle("key", { key: "SpaceBar", mode: "press" });
  assert.ok(game.down.has("Space"));
  await game.bridge.handle("key", { mode: "release_all" });
  assert.ok(!game.down.has("Space"));
  game.pump(5);
  await pressed;
  assert.deepEqual(game.keyLog, ["keydown:Space", "keyup:Space"]); // (one keyup only)

  const g2 = makeFakeGame();
  g2.bridge.handle("key", { key: "E", mode: "press" }).catch(() => {});
  g2.bridge.handle("input", { action: "jump" }).catch(() => {});
  assert.ok(g2.down.has("KeyE") && g2.down.has("Space"));
  g2.bridge.close();
  assert.ok(!g2.down.has("KeyE") && !g2.down.has("Space"));

  // An agent that leaves lets go of its taps too
  const { sockets, WebSocket } = fakeSockets();
  const g3 = makeFakeGame({ WebSocket });
  g3.bridge.connect();
  sockets[0].open();
  sockets[0].request(1, "key", { key: "Q", mode: "press" });
  assert.ok(g3.down.has("KeyQ"));
  sockets[0].close();
  assert.ok(!g3.down.has("KeyQ"));
  g3.bridge.close();
});

test("a stick press stays until the game reads the pad, then clears", async () => {
  const game = makeFakeGame();
  const pad = game.bridge.pad;
  let done = false;
  const pressed = game.bridge.handle("key", { key: "Gamepad_LeftX", mode: "press", value: 1 }).then(() => (done = true));
  assert.equal(pad.axes[0], 1); // (not cleared in the same call)
  game.pump(3);
  await settle();
  assert.equal(pad.axes[0], 1); // the game has not read the pad yet
  assert.equal(done, false);
  assert.equal(game.nav.getGamepads()[0].axes[0], 1); // the game's sample sees the press
  game.pump(1);
  await pressed;
  assert.equal(pad.axes[0], 0);
  assert.equal(game.nav.getGamepads().length, 0); // (idle: listed no more)

  // A game that never reads the pad: the press clears after PAD_PRESS_MAX_MS
  const g2 = makeFakeGame();
  const p2 = g2.bridge.handle("key", { key: "Gamepad_LeftY", mode: "press", value: 1 });
  assert.equal(g2.bridge.pad.axes[1], -1);
  g2.pump(70); // 1120 ms
  await p2;
  assert.equal(g2.bridge.pad.axes[1], 0);

  // A press over a hold of the same stick: when the press ends, the stick takes the hold's value again
  const g3 = makeFakeGame();
  await g3.bridge.handle("key", { key: "Gamepad_LeftX", mode: "hold", value: 0.25, lease: 10 });
  const p3 = g3.bridge.handle("key", { key: "Gamepad_LeftX", mode: "press", value: 1 });
  assert.equal(g3.bridge.pad.axes[0], 1);
  g3.nav.getGamepads();
  g3.pump(1);
  await p3;
  assert.equal(g3.bridge.pad.axes[0], 0.25);
  await g3.bridge.handle("key", { key: "Gamepad_LeftX", mode: "release" });
  assert.equal(g3.bridge.pad.axes[0], 0);

  // An explicit hold and release are as before
  await game.bridge.handle("key", { key: "Gamepad_LeftX", mode: "hold", value: -1 });
  game.nav.getGamepads();
  game.pump(3);
  assert.equal(pad.axes[0], -1);
  await game.bridge.handle("key", { key: "Gamepad_LeftX", mode: "release" });
  assert.equal(pad.axes[0], 0);
});

test("a key that a hold and a tap share goes up only when the last owner lets go", async () => {
  // A hold of the action push (KeyW) and a tap of W share KeyW
  const game = makeFakeGame();
  await game.bridge.handle("input", { action: "push", mode: "hold" });
  const tapped = game.bridge.handle("key", { key: "W", mode: "press" });
  game.pump(5);
  await tapped;
  assert.ok(game.down.has("KeyW")); // the tap's end does not end the hold
  assert.deepEqual(game.keyLog, ["keydown:KeyW"]);
  await game.bridge.handle("input", { action: "push", mode: "release" });
  assert.deepEqual(game.keyLog, ["keydown:KeyW", "keyup:KeyW"]);

  // A tap first, then a hold while the tap is in flight
  const g2 = makeFakeGame();
  const t2 = g2.bridge.handle("key", { key: "W", mode: "press" });
  await g2.bridge.handle("key", { key: "W", mode: "hold", lease: 10 });
  g2.pump(5);
  await t2;
  assert.ok(g2.down.has("KeyW"));
  await g2.bridge.handle("key", { key: "W", mode: "release" });
  assert.deepEqual(g2.keyLog, ["keydown:KeyW", "keyup:KeyW"]);

  // An agent's hold and a game hook's hold of the same key
  const g3 = makeFakeGame();
  await g3.bridge.handle("key", { key: "W", mode: "hold", lease: 10 });
  g3.hooks.move = async ({ keys }) => {
    keys.hold("W");
    await keys.frame();
    keys.release("W");
  };
  const moving = g3.bridge.handle("move", { direction: { X: 1, Y: 0, Z: 0 }, seconds: 0.1 });
  g3.pump(2);
  await moving;
  assert.ok(g3.down.has("KeyW")); // the agent still holds W
  await g3.bridge.handle("key", { key: "W", mode: "release" });
  assert.ok(!g3.down.has("KeyW"));
});

test("close() ends the ops still waiting; an op that ended before keeps its result", async () => {
  const game = makeFakeGame();
  const early = game.bridge.handle("watch", { objects: ["/Scene/Rail"], seconds: 0 });
  game.pump(1);
  assert.deepEqual(await early, { changes: [] });
  const w = game.bridge.handle("watch", { objects: ["/Scene/Player"], seconds: 60 });
  game.pump(2);
  game.bridge.close();
  await assert.rejects(w, /the bridge is closed/);
  await assert.rejects(game.bridge.handle("ping", {}), /the bridge is closed/);
});

// -- the review findings of William-Hill/atl-arcade#388 --------------------------------------------------------------

test("connect() keeps a socket that is open or opening; a new one comes after it closes", () => {
  const { sockets, WebSocket } = fakeSockets();
  const game = makeFakeGame({ WebSocket });
  game.bridge.connect();
  game.bridge.connect(); // (opening: no second socket)
  assert.equal(sockets.length, 1);
  sockets[0].open();
  game.bridge.connect(); // (open: no second socket)
  assert.equal(sockets.length, 1);
  sockets[0].close();
  game.bridge.connect();
  assert.equal(sockets.length, 2);
  game.bridge.close();
  assert.equal(sockets[1].readyState, 3); // close() closes the one live socket
});

test("2 fallback moves that hold the same key each let go of their own hold", async () => {
  const game = makeFakeGame();
  const long = game.bridge.handle("move", { direction: { X: 1, Y: 0, Z: 0 }, seconds: 0.3, via: "keys" });
  const short = game.bridge.handle("move", { direction: { X: 1, Y: 0, Z: 0 }, seconds: 0.05, via: "keys" });
  await short;
  assert.ok(game.down.has("KeyW")); // the long move still holds W
  await long;
  assert.ok(!game.down.has("KeyW"));
  assert.deepEqual(game.keyLog, ["keydown:KeyW", "keyup:KeyW"]);
});

test("a move with a bad move key fails before any key goes down", async () => {
  const game = makeFakeGame({ options: { moveKeys: { right: "Keyd" } } });
  // forward and right: W and the bad key
  await assert.rejects(game.bridge.handle("move", { direction: { X: 1, Y: 1, Z: 0 }, seconds: 0.05, via: "keys" }), /Unknown move key Keyd/);
  assert.deepEqual(game.keyLog, []);
  assert.equal(game.down.size, 0);
});

test("close() ends a fallback move that still waits, and lets go of its keys", async () => {
  const game = makeFakeGame();
  const started = Date.now();
  const moving = game.bridge.handle("move", { direction: { X: 1, Y: 0, Z: 0 }, seconds: 30, via: "keys" });
  assert.ok(game.down.has("KeyW"));
  game.bridge.close();
  await assert.rejects(moving, /the bridge is closed/);
  assert.ok(Date.now() - started < 5000);
  assert.ok(!game.down.has("KeyW"));
  // A move that ended before the close keeps its result
  const g2 = makeFakeGame();
  const r = await g2.bridge.handle("move", { direction: { X: 1, Y: 0, Z: 0 }, seconds: 0.02, via: "keys" });
  assert.equal(r.via, "keys");
  g2.bridge.close();
});

test("watch compares a turn across the 180 degree line by the short way", async () => {
  const game = makeFakeGame();
  const spinner = add(game.scene, object("Spinner", "Group", [0, 0, -2]));
  // A yaw of `deg` (kit): forward [sin, 0, -cos], so the matrix' z column is [-sin, 0, cos]
  const yaw = (deg) => {
    const r = (deg * Math.PI) / 180;
    const e = spinner.matrixWorld.elements;
    [e[0], e[2], e[8], e[10]] = [Math.cos(r), Math.sin(r), -Math.sin(r), Math.cos(r)];
  };
  yaw(179.8);
  const small = game.bridge.handle("watch", { objects: ["/Scene/Spinner"], seconds: 0.05 });
  yaw(-179.8); // 0.4 degrees
  game.pump(5);
  assert.deepEqual((await small).changes, []);
  const big = game.bridge.handle("watch", { objects: ["/Scene/Spinner"], seconds: 0.05 });
  yaw(170); // 10.2 degrees
  game.pump(5);
  assert.deepEqual((await big).changes.map((c) => c.what), ["turned"]);
});

test("an rc path with a banned part fails with 403, also after a missing part", async () => {
  const game = makeFakeGame();
  const rc = (body) => game.bridge.handle("rc", { verb: "property", body });
  for (const body of [
    { objectPath: "state.nothing.constructor" },
    { objectPath: "nothing.__proto__" },
    { objectPath: "state", propertyName: "nothing.constructor" },
    { objectPath: "state", propertyName: "nothing:prototype" },
  ])
    await assert.rejects(rc(body), (e) => e.status === 403 && /does not follow/.test(e.message), JSON.stringify(body));
  // A missing path with no banned part is still 404
  await assert.rejects(rc({ objectPath: "state.nothing.deeper" }), (e) => e.status === 404);
  await assert.rejects(rc({ objectPath: "state", propertyName: "nothing.deeper" }), (e) => e.status === 404);
});

test("an rc write never writes a property name with a dot or a colon", async () => {
  const state = { inner: { x: 1 } };
  const game = makeFakeGame({ options: { hooks: { player: () => null, objects: { state }, rcWritable: ["state.inner.x", "state.inner"] } } });
  const write = (objectPath, propertyName) =>
    game.bridge.handle("rc", { verb: "property", body: { objectPath, propertyName, propertyValue: { [propertyName]: 5 }, access: "WRITE_ACCESS" } });
  await assert.rejects(write("state", "inner.x"), /not writable/);
  await assert.rejects(write("state", "inner:x"), /not writable/);
  assert.equal(state["inner.x"], undefined);
  await write("state.inner", "x"); // (the declared object and property)
  assert.equal(state.inner.x, 5);
});

test("record start fails, and records nothing, when the picture canvas goes away after the probe", async () => {
  const pic = { width: 320, height: 200 };
  let calls = 0;
  const doc = {
    createElement: () => ({ getContext: () => ({ drawImage() {} }), toDataURL: () => "data:image/jpeg;base64,AA" }),
  };
  const { sockets, WebSocket } = fakeSockets();
  const game = makeFakeGame({ WebSocket, options: { document: doc, pictureCanvas: () => (calls++ === 0 ? pic : null) } });
  await assert.rejects(game.bridge.handle("record", { action: "start" }), (e) => e.status === 409 && /no picture/.test(e.message));
  // The canvas comes back: no recording is on, so no frame goes out
  calls = 0;
  game.bridge.connect();
  sockets[0].open();
  for (let i = 0; i < 20; i += 1) {
    calls = 0;
    game.pump(1);
  }
  assert.deepEqual(sockets[0].sent.filter((f) => f.type === "frame"), []);
  game.bridge.close();
});
