# Autopilot for Three.js (a game in the web browser)

`autopilot-bridge.js` is one ES module with no dependencies. A Three.js game imports it and calls `connectBridge()`.
The bridge connects out to the agent service (`ws://127.0.0.1:8765/v1/bridge`), says hello as engine `threejs`, and
answers the same engine-neutral ops as the Unity and Godot plugins. The brain's side is
`autopilot/engines/threejs.py`, with its kit notes in `autopilot/engines/threejs.md`.

Status: experimental. It is not an editor plugin: there is no Autopilot tab, no first-run setup, and no codex export.
Start the agent service yourself, and open the game's page with the bridge turned on.

## Add it to a game

Three.js has no standard player, movement, or UI. The game gives them to the bridge as hooks. Load the bridge only
when you ask for it, so that a player's build never connects:

```js
if (new URLSearchParams(location.search).get("autopilot") === "1") {
  const { connectBridge } = await import("./autopilot-bridge.js");
  connectBridge({
    url: new URLSearchParams(location.search).get("autopilot_url") ?? undefined,
    scene, camera, canvas: renderer.domElement,
    project: "my-game", // package.json's name: the adapter keys the codex and memory by it
    hooks: {
      player: () => ({ object: playerMesh, position: [...], velocity: [...], forward: [...], onGround: true, halfHeight: 0.9 }),
      movement: () => ({ walkSpeed: 5, jumpSpeed: 6, gravity: 9.8 }),
      controls: () => [{ name: "jump", keys: ["Space"] }],
      restart: () => game.reset(),
      render: () => renderer.render(scene, camera),
      objects: { state: game.state },
    },
  });
}
```

The bridge connects only to an agent service on this machine (`ws://127.0.0.1`, `localhost` or `[::1]`). Whatever it
connects to can read the game, press its keys and call its functions, so a link with another host in `autopilot_url`
must not take a player's game over: the bridge refuses such an address (state `refused`) unless the game itself
passes `allowRemote: true`. The browser's WebSocket cannot send an `Authorization` header, so the bridge sends no token.

`connectBridge`'s comment in the module lists every option and hook. Only `player` is needed for the kit's player
reads; without `restart`, `session` restart answers 501; without `render`, a canvas made without
`preserveDrawingBuffer` can read back black.

## What it answers

| Op | How |
| --- | --- |
| `hello` | `engine: "threejs"`, `share: false` (a browser game never shares runs' data), features `timed_hold`, `levels`, `pointer`, `media` |
| `find`, `transforms` | the scene graph by path (`/Scene/Level/Rail#2`); classes from the type, `userData.class` or the `classOf` hook |
| `pose`, `movement` | the `player` and `movement` hooks; yaw and pitch from the camera |
| `view`, `watch` | frustum and box tests on the scene's things; no line-of-sight test |
| `key`, `input`, `move`, `pointer` | KeyboardEvents on the page, a virtual standard gamepad in `navigator.getGamepads()`, pointer and mouse events on the canvas; `move` through the `move` hook, else W/A/S/D relative to the camera |
| `screen`, `ui`, `ui_act` | the page's DOM |
| `rc` | `property`, `call`, `describe` on the scene's objects and on the `objects` hook |
| `capture`, `record` | the canvas as JPEG, after the `render` hook |
| `session`, `levels`, `pause`, `ping`, `context` | the `restart`, `levels`, `pause` hooks |
| `codex_manifest`, `codex_read` | not answered (`unknown op`): the brain copies the codex from the project folder on disk |

## Headless

`headless.mjs` opens a page that a dev server already serves, in headless Chromium, with `?autopilot=1` and the
agent service's address. It uses the Playwright that the game's project has:

```bash
node plugin/threejs/headless.mjs --project path/to/game --page http://localhost:5173/ \
  --bridge ws://127.0.0.1:8765/v1/bridge --log game.log
```

## Try it without a model

`tools/web_probe.py` waits for the page, runs each op once through the kit, and writes `report.json` and a still. It
uses no decision model, no language model, and no key:

```bash
AUTOPILOT_SHARE=0 python tools/web_probe.py --out probe/
```

## Tests

- `node --test plugin/threejs/test/*.test.mjs`: the bridge's ops on a stand-in game (`test/fake_game.mjs`), with no browser.
- `pytest tests/test_threejs.py`: the adapter, and the bridge over a real WebSocket in Node (Node 22 or later).
