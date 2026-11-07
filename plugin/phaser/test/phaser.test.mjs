// The Phaser helper (autopilot-phaser.js) with the browser bridge on a stand-in Phaser game (fake_phaser.mjs), without a
// socket: `node --test plugin/phaser/test/*.test.mjs`.

import { test } from "node:test";
import assert from "node:assert/strict";
import { makeFrame2D, unrealName } from "../../threejs/autopilot-bridge.js";
import { affineOf, cameraOf, localBox, nativeMatrix, phaserBridgeOptions } from "../autopilot-phaser.js";
import { camera, container, fakeCanvas, gameObject, makeFakePhaser } from "./fake_phaser.mjs";

const close = (a, b, eps = 1e-9) => Math.abs(a - b) < eps;

test("the 2D frame is the Godot plugin's: kit = (x, 0, y) with y up", () => {
  const f = makeFrame2D(1);
  assert.deepEqual(f.kit([100, -400, 0]), { X: 100, Y: 0, Z: -400 });
  assert.deepEqual(f.fromKit({ X: 100, Y: 0, Z: -400 }), [100, -400, 0]);
  assert.equal(f.yawOf([1, 0, 0]), 0);
  assert.equal(f.yawOf([-1, 0, 0]), 180);
  assert.equal(f.yawOf([0, 0, -1]), 0); // (the camera, looking at the plane)
  assert.equal(f.pitchOf([1, 1, 0]), 45);
  assert.deepEqual(makeFrame2D(0.5).kit([100, 0, 0]), { X: 50, Y: 0, Z: 0 });
});

test("affineOf composes Containers; nativeMatrix flips y", () => {
  const child = gameObject("Sprite", { x: 10, y: 0 });
  container({ x: 100, y: 50, rotation: Math.PI / 2, scaleX: 2, scaleY: 2 }, [child]);
  const m = affineOf(child);
  // (10, 0) turned a quarter clockwise (y down) and doubled: (0, 20), then moved by (100, 50)
  assert.ok(close(m.tx, 100) && close(m.ty, 70), JSON.stringify(m));
  const e = nativeMatrix(m);
  assert.ok(close(e[12], 100) && close(e[13], -70) && e[14] === 0);
  // a quarter clockwise on screen is a quarter clockwise with y up too: the x axis points down
  assert.ok(close(e[0], 0) && close(e[1], -2));
});

test("localBox follows the origin; cameraOf is orthographic on the world view", () => {
  assert.deepEqual(localBox(gameObject("Sprite", { width: 32, height: 64, originX: 0.5, originY: 1 })), { min: { x: -16, y: 0, z: 0 }, max: { x: 16, y: 64, z: 0 } });
  assert.equal(localBox(gameObject("Container")), null);
  const cam = cameraOf(camera(100, 0, 960, 540));
  assert.deepEqual(cam.matrixWorld.elements.slice(12, 14), [580, -270]);
  assert.equal(cam.viewWidth, 960);
});

test("hello and context say engine phaser, 2D", async () => {
  const { bridge } = makeFakePhaser();
  const h = bridge.hello();
  assert.equal(h.engine, "phaser");
  assert.equal(h.engine_version, "4.2.0");
  assert.equal(h.share, false);
  const c = await bridge.handle("context", {});
  assert.equal(c.engine, "phaser");
  assert.equal(c.dimension, "2d");
  assert.equal(c.character, "/Game/Level/player");
  assert.deepEqual(c.controls.map((a) => [a.name, a.keys]), [["right", ["D", "Right"]], ["jump", ["SpaceBar"]]]);
});

test("find: things are a scene's objects by path; classes from the type or the Data Manager", async () => {
  const { bridge } = makeFakePhaser();
  const all = (await bridge.handle("find", {})).objects;
  assert.ok(all.includes("/Game/Level/player") && all.includes("/Game/Level/coins/coin#2") && all.includes("/Game/HUD/score"));
  const things = (await bridge.handle("find", { things: true })).objects;
  assert.ok(things.includes("/Game/Level/player") && things.includes("/Game/Level/coins"));
  assert.ok(!things.includes("/Game/Level")); // (a scene is not a thing)
  assert.deepEqual((await bridge.handle("find", { class: "Coin" })).objects, ["/Game/Level/coins/coin", "/Game/Level/coins/coin#2"]);
  assert.deepEqual((await bridge.handle("find", { class: "Image" })).objects, ["/Game/Level/crate"]);
  await assert.rejects(bridge.handle("find", { class: "Dragon" }), /Unknown class Dragon/);
});

test("transforms: kit X is right, kit Z is up (-y), 1 pixel = 1 unit; a clockwise turn is a pitch down", async () => {
  const { bridge } = makeFakePhaser();
  const t = (await bridge.handle("transforms", { objects: ["/Game/Level/coins/coin#2", "/Game/Level/crate", "/Game/Level/nope"] })).objects;
  assert.deepEqual(t["/Game/Level/coins/coin#2"].location, { X: 400, Y: 0, Z: -300 });
  assert.deepEqual(t["/Game/Level/coins/coin#2"].extent, { X: 8, Y: 0, Z: 8 });
  assert.equal(t["/Game/Level/crate"].rotation.Pitch, -90);
  assert.equal(t["/Game/Level/nope"], null);
});

test("pose reads the player's game object and its Arcade body", async () => {
  const fake = makeFakePhaser();
  let pose = await fake.bridge.handle("pose", {});
  assert.equal(pose.path, "/Game/Level/player");
  assert.deepEqual(pose.feet, { X: 100, Y: 0, Z: -400 }); // (origin at its feet: the bottom middle)
  assert.deepEqual(pose.at, { X: 100, Y: 0, Z: -368 });
  assert.equal(pose.on_ground, true);
  assert.equal(pose.body_yaw, 0);
  assert.equal(pose.state, "running");
  fake.player.body.velocity = { x: -50, y: -300 };
  fake.player.flipX = true;
  fake.player.body.blocked.down = false;
  pose = await fake.bridge.handle("pose", {});
  assert.deepEqual(pose.velocity, { X: -50, Y: 0, Z: 300 }); // (up on screen is kit +Z)
  assert.equal(pose.body_yaw, 180);
  assert.equal(pose.on_ground, false);
  const m = await fake.bridge.handle("movement", {});
  assert.equal(m.walk_speed, 200);
  assert.equal(m.gravity, 1200);
});

test("view lists what the camera shows, never the player; HUD objects where they show, with their text", async () => {
  const { bridge } = makeFakePhaser();
  const v = await bridge.handle("view", {});
  const byPath = Object.fromEntries(v.visible.map((i) => [i.path, i]));
  assert.ok(byPath["/Game/Level/coins/coin#2"] && byPath["/Game/Level/crate"]);
  assert.ok(!byPath["/Game/Level/player"]);
  assert.equal(byPath["/Game/HUD/score"].label, "Score 0");
  assert.ok(byPath["/Game/HUD/score"].screen_x < -0.9 && byPath["/Game/HUD/score"].screen_y > 0.9); // (top left)
  assert.equal(byPath["/Game/Level/lives"].label, "Lives 3");
  assert.deepEqual(v.camera, { location: { X: 480, Y: 0, Z: -270 }, yaw: 0, pitch: 0, width: 960, height: 540 });
  const d = v.visible.map((i) => i.distance);
  assert.deepEqual(d, [...d].sort((a, b) => a - b));
});

test("a HUD object (scroll factor 0) stays on screen as the camera scrolls; hud: false leaves it out", async () => {
  const fake = makeFakePhaser();
  fake.level.cameras.main.worldView.x = 1000;
  const t = (await fake.bridge.handle("transforms", { objects: ["/Game/Level/lives", "/Game/HUD/score"] })).objects;
  assert.deepEqual(t["/Game/Level/lives"].location, { X: 1900, Y: 0, Z: -20 });
  assert.deepEqual(t["/Game/HUD/score"].location, { X: 1020, Y: 0, Z: -20 });
  const bare = makeFakePhaser({ options: { hud: false } });
  const all = (await bare.bridge.handle("find", {})).objects;
  assert.ok(!all.includes("/Game/Level/lives") && !all.includes("/Game/HUD/score"));
  const one = makeFakePhaser({ options: { scenes: ["Level"] } });
  assert.ok(!(await one.bridge.handle("find", {})).objects.some((p) => p.startsWith("/Game/HUD")));
});

test("move in 2D holds D for screen right and says how far the player ran", async () => {
  const fake = makeFakePhaser();
  const moving = fake.bridge.handle("move", { direction: { X: 1, Y: 0, Z: 0 }, seconds: 0.2 });
  const timer = setInterval(() => fake.pump(1), 16);
  const r = await moving;
  clearInterval(timer);
  assert.equal(r.via, "keys");
  assert.ok(r.moved > 0, JSON.stringify(r));
  assert.ok(fake.keyLog.includes("keydown:KeyD") && fake.keyLog.includes("keyup:KeyD"));
  assert.ok(!fake.keyLog.some((k) => k.endsWith("KeyW")));
});

test("a move hook gets the direction in Phaser's axes (y down); moveKeys changes the keys", async () => {
  let got = null;
  const fake = makeFakePhaser({ options: { hooks: { player: () => ({ object: null, x: 0, y: 0 }), move: (a) => (got = a) } } });
  const r = await fake.bridge.handle("move", { direction: { X: 0, Y: 0, Z: 1 }, seconds: 0.01 });
  assert.equal(r.via, "game");
  assert.deepEqual(got.direction.slice(0, 2), [0, -1]); // (up on screen)
  const arrows = makeFakePhaser({ options: { moveKeys: { up: "Up", down: "Down", left: "Left", right: "Right" } } });
  const moving = arrows.bridge.handle("move", { direction: { X: -1, Y: 0, Z: 0 }, seconds: 0.05 });
  const timer = setInterval(() => arrows.pump(1), 16);
  await moving;
  clearInterval(timer);
  assert.ok(arrows.keyLog.includes("keydown:ArrowLeft"));
});

test("rc reads the game objects themselves; a game object in a value is its path", async () => {
  const fake = makeFakePhaser();
  const rc = (body) => fake.bridge.handle("rc", { verb: "property", body });
  assert.deepEqual(await rc({ objectPath: "/Game/Level/player", propertyName: "x" }), { x: 100 });
  assert.deepEqual(await rc({ objectPath: "state", propertyName: "target" }), { target: "/Game/Level/coins/coin#2" });
  await fake.bridge.handle("rc", { verb: "call", body: { objectPath: "game", functionName: "addPoints", parameters: { points: 5 } } });
  assert.equal(fake.state.score, 5);
});

test("frames: a WebGL game without preserveDrawingBuffer is copied on postrender", async () => {
  const fake = makeFakePhaser();
  const r = await fake.bridge.handle("record", { action: "start", fps: 5, max_dim: 480 });
  assert.deepEqual(r, { recording: true, width: 480, height: 270 });
  await fake.bridge.handle("record", { action: "stop" });
  const kept = makeFakePhaser();
  kept.game.config.preserveDrawingBuffer = true;
  // (a canvas that keeps its picture is read as it is: no copy)
  const opts = phaserBridgeOptions(kept.game, { document: { createElement: () => assert.fail("no copy") } });
  kept.pump(2);
  assert.equal(opts.pictureCanvas(), kept.canvas);
});

test("the copy starts when the WebGL renderer shows up, even after the helper was called", () => {
  const fake = makeFakePhaser();
  const renderer = fake.game.renderer;
  fake.game.renderer = undefined; // (the game has not booted yet)
  const opts = phaserBridgeOptions(fake.game, { document: { createElement: () => fakeCanvas() } });
  fake.pump(1);
  assert.equal(opts.pictureCanvas(), fake.canvas);
  fake.game.renderer = renderer;
  fake.pump(1);
  assert.notEqual(opts.pictureCanvas(), fake.canvas);
  assert.equal(opts.pictureCanvas().width, 960);
});

test("while the game is paused, a change shows in the next op; a restart still answers", async () => {
  const fake = makeFakePhaser();
  await fake.bridge.handle("pause", { paused: true });
  fake.player.x = 250; // (no frame runs: the loop is paused)
  const t = (await fake.bridge.handle("transforms", { objects: ["/Game/Level/player"] })).objects;
  assert.equal(t["/Game/Level/player"].location.X, 250);
  const r = await fake.bridge.handle("session", { action: "restart" }); // (no frame comes: the wait ends by time)
  assert.equal(r.running, true);
  assert.equal(fake.level.restarts, 1);
});

test("the player hook may give its feet's x alone; the rest comes from the game object", async () => {
  const fake = makeFakePhaser({ options: { hooks: { player: () => ({ object: fake.player, x: 140 }) } } });
  const p = await fake.bridge.handle("pose", {});
  assert.deepEqual(p.feet, { X: 140, Y: 0, Z: -400 });
  assert.deepEqual(makeFrame2D(0).kit([5, 0, 0]), { X: 5, Y: 0, Z: 0 }); // (a scale of 0 or less is 1)
});

test("restart, levels and pause default to the camera scene's and the game's own", async () => {
  const fake = makeFakePhaser();
  fake.player.x = 700;
  const restarting = fake.bridge.handle("session", { action: "restart" });
  fake.pump(3);
  const r = await restarting;
  assert.equal(r.running, true);
  assert.equal(fake.level.restarts, 1);
  assert.equal(fake.player.x, 100);
  const going = fake.bridge.handle("session", { action: "restart", map: "Boss" });
  fake.pump(3);
  await going;
  assert.equal(fake.level.started, "Boss");
  assert.deepEqual(await fake.bridge.handle("levels", {}), { levels: ["Boot", "Level", "HUD"], open: "Level", playing: "Level" });
  assert.deepEqual(await fake.bridge.handle("pause", { paused: true }), { paused: true });
  assert.equal(fake.game.isPaused, true);
  await fake.bridge.handle("pause", { paused: false });
  assert.equal(fake.game.isPaused, false);
});

test("a HUD Container's objects move with it; a hidden player keeps its feet", async () => {
  const label = gameObject("Text", { name: "label", x: 10, y: 0, width: 40, height: 20, text: "Gems 2" });
  const panel = container({ name: "panel", x: 100, y: 50, scrollFactorX: 0, scrollFactorY: 0 }, [label]);
  const fake = makeFakePhaser();
  fake.level.children.list.push(panel);
  fake.level.cameras.main.worldView.x = 1000;
  let t = (await fake.bridge.handle("transforms", { objects: ["/Game/Level/panel/label"] })).objects;
  assert.deepEqual(t["/Game/Level/panel/label"].location, { X: 1110, Y: 0, Z: -50 }); // (on screen at 110, 50)
  fake.player.visible = false; // (blinking after a hit)
  const p = await fake.bridge.handle("pose", {});
  assert.deepEqual(p.feet, { X: 100, Y: 0, Z: -400 });
});

test("game time never goes back on a restart and stops while paused", async () => {
  const fake = makeFakePhaser();
  fake.pump(10);
  const t0 = (await fake.bridge.handle("ping", {})).game_time ?? (await fake.bridge.handle("context", {})).game_time;
  const restarting = fake.bridge.handle("session", { action: "restart" });
  fake.pump(3);
  await restarting;
  const t1 = (await fake.bridge.handle("context", {})).game_time;
  assert.ok(t1 > t0, `${t1} > ${t0}`);
  await fake.bridge.handle("pause", { paused: true });
  fake.pump(5);
  assert.equal((await fake.bridge.handle("context", {})).game_time, t1);
});

test("a game object out of the scenes read has no node; the copy runs at most mirrorFps a second", async () => {
  const fake = makeFakePhaser();
  const opts = phaserBridgeOptions(fake.game, { scenes: ["HUD"] });
  assert.equal(opts.nodeOf(fake.player), null);
  assert.ok(opts.nodeOf(fake.score));
  let copies = 0;
  const counting = makeFakePhaser({ options: { document: { createElement: () => ({ width: 0, height: 0, getContext: () => ({ drawImage: () => (copies += 1) }) }) } } });
  counting.pump(63); // (about 1 s of 16 ms frames)
  assert.ok(copies >= 9 && copies <= 11, String(copies));
});

test("arrow keys keep their Unreal names", () => {
  assert.equal(unrealName("ArrowRight"), "Right");
  assert.equal(unrealName("ArrowLeft"), "Left");
  assert.equal(unrealName("ShiftRight"), "RightShift");
});
