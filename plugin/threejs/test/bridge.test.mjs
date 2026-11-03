// The bridge's ops on a stand-in game (fake_game.mjs), without a socket: `node --test plugin/threejs/test/*.test.mjs`.

import { test } from "node:test";
import assert from "node:assert/strict";
import { keySpec, makeFrame, project, unrealName, worldBox } from "../autopilot-bridge.js";
import { camera, makeFakeGame, object } from "./fake_game.mjs";

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
