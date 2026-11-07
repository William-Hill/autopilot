# Autopilot for Phaser (a 2D game in the web browser)

`autopilot-phaser.js` is one ES module with no dependencies. It lets a Phaser game (Phaser 3.60 or later, or Phaser 4)
use Autopilot's browser bridge, [`../threejs/autopilot-bridge.js`](../threejs/README.md). The bridge connects out to
the agent service (`ws://127.0.0.1:8765/v1/bridge`), says hello as engine `phaser`, and answers the same
engine-neutral ops as the Unity and Godot plugins, in 2D. The brain's side is `autopilot/engines/phaser.py`, with its
kit notes in `autopilot/engines/phaser.md`.

Status: experimental, as the Three.js bridge. It is not an editor plugin: there is no Autopilot tab, no first-run
setup, and no codex export. Start the agent service yourself, and open the game's page with the bridge turned on.

## How it works

The bridge reads a Three.js scene graph: objects with `name`, `type`, `children`, `matrixWorld.elements` and
`geometry.boundingBox`, and a camera with `matrixWorldInverse` and `projectionMatrix`. Phaser has no such graph.
`phaserBridgeOptions(game, options)` builds one from the active Phaser scenes once a game frame, and returns the
options for the bridge's `connectBridge()`:

- **Things**: `/Game/<scene key>/<object>`, a Container's objects under it. Each object's box comes from its size and
  origin, and its transform from its position, rotation, scale and parent Containers.
- **The 2D frame**: the Godot plugin's. Kit X is screen right, kit Z is screen up, kit Y is 0, 1 pixel is 1 kit unit:
  `kit (X, Y, Z) = (x, 0, -y)`. A clockwise turn on screen is a pitch down.
- **The camera**: an orthographic camera on the world view of the camera scene's main camera.
- **Screen-space objects** (scroll factor 0, or in another scene such as a HUD scene) are placed where they show over
  the world, so `view` lists them with their `label` (a Text's text).
- **Frames**: a WebGL canvas made without `preserveDrawingBuffer` reads back blank after the browser shows it. The
  helper copies each frame on Phaser's `postrender` event, while the buffer still holds it, and the bridge reads the
  copy. With `preserveDrawingBuffer: true` (or the Canvas renderer), the bridge reads the game's canvas.

## Add it to a game

Copy `plugin/threejs/autopilot-bridge.js` and `plugin/phaser/autopilot-phaser.js` into the game's source. Load them only
when you ask for them, so that a player's build never connects:

```js
const params = new URLSearchParams(location.search);
if (params.get("autopilot") === "1") {
  const { connectBridge } = await import("./autopilot-bridge.js");
  const { phaserBridgeOptions } = await import("./autopilot-phaser.js");
  connectBridge(phaserBridgeOptions(game, {
    url: params.get("autopilot_url") ?? undefined,
    project: "my-game", // package.json's name: the adapter keys the codex and memory by it
    phaser: Phaser,     // its VERSION goes into hello
    scenes: ["Level"],  // optional: the scenes to read (every active scene by default)
    moveKeys: { up: "Up", down: "Down", left: "Left", right: "Right" }, // optional: `move` without a move hook
    hooks: {
      player: () => ({ object: level.player, state: level.player.state }),
      movement: () => ({ walkSpeed: 200, jumpSpeed: 520, gravity: 1200 }), // pixels a second, and a second squared
      controls: () => [{ name: "jump", keys: ["Space", "ArrowUp"] }, { name: "right", keys: ["ArrowRight"] }],
      objects: { state: level.state },
    },
  }));
}
```

`phaserBridgeOptions`' comment in the module lists every option. The hooks are `connectBridge`'s, with Phaser's axes
(pixels, y down) where they give positions:

| Hook | Default | What it gives |
| --- | --- | --- |
| `player()` | none | `{object}`: the player's game object. Its feet are the bottom middle of its box, its velocity and `onGround` its Arcade body's, its facing its `flipX`; `x`, `y`, `velocity`, `onGround`, `facing`, `halfHeight` and `state` override them |
| `move({direction, seconds, scale, keys})` | keys held screen-relative (`moveKeys`, W/A/S/D) | walk the player that way the game's own way; `direction` is `[x, y]` with y down |
| `restart({map})` | the camera scene's `scene.restart()`, or `scene.start(map)` | start the level over, or another one |
| `levels()` | every scene key, the camera scene's playing | `{levels, playing}` |
| `pause(paused)`, `paused()` | `game.pause()` and `game.resume()` | pause or resume |
| `gameTime()` | the camera scene's clock | seconds of game time |
| `movement`, `controls`, `objects`, `classOf`, `render`, `uiRoot` | as `connectBridge` | see [`../threejs/README.md`](../threejs/README.md) |

A game object's Data Manager gives what a Three.js object keeps in `userData`: `setData("class", "Coin")` gives it a
class for `find` (and makes it a thing at any depth), `setData("thing", true)` makes it a thing, and
`setData("label", "door")` labels it in `view`.

The bridge connects only to an agent service on this machine (`ws://127.0.0.1`, `localhost` or `[::1]`), unless the
game passes `allowRemote: true`. See [`../threejs/README.md`](../threejs/README.md) for why.

## What it answers

The same ops as the Three.js bridge ([`../threejs/README.md`](../threejs/README.md#what-it-answers)), with these
differences:

| Op | How |
| --- | --- |
| `hello`, `context` | `engine: "phaser"`, `engine_version`: Phaser's `VERSION`, `dimension: "2d"` |
| `find`, `transforms` | the game objects by path (`/Game/Level/coins/coin#2`); classes from the type, the Data Manager's `class` or the `classOf` hook; the objects directly in a scene are things |
| `pose`, `movement` | the `player` hook and the player's Arcade body; yaw and pitch 0 (the camera looks at the plane) |
| `view` | the boxes in the camera's world view, nearest its middle first; `camera` gives `width` and `height`, not a field of view |
| `move` | the `move` hook, else keys held screen-relative: kit +X right, kit +Z up |
| `rc` | a game object's path reaches the game object itself (`x`, `y`, `body`...); a game object in a value answers its path |
| `capture`, `record` | the copy of the last frame (WebGL without `preserveDrawingBuffer`), else the canvas |
| `session`, `levels`, `pause` | the hooks, else the Phaser scene manager and `game.pause()` |

## Headless

As the Three.js bridge's: `plugin/threejs/headless.mjs` opens a page that a dev server already serves, in headless
Chromium, with `?autopilot=1` and the agent service's address, and the brain's `launch_command` for engine `phaser` runs
it. With a GPU-less headless Chromium, Phaser may fall back to its Canvas renderer: the frames still read.

## Try it without a model

`tools/web_probe.py` waits for the page, runs each op once through the kit, and writes `report.json` and a still. It
uses no decision model, no language model, and no key. For a Phaser game, give it a key that moves the player:

```bash
AUTOPILOT_SHARE=0 python tools/web_probe.py --out probe/ --key D --class Sprite
```

## Tests

- `node --test plugin/phaser/test/*.test.mjs`: the helper and the bridge's 2D ops on a stand-in Phaser game
  (`test/fake_phaser.mjs`), with no browser and no Phaser.
- `pytest tests/test_phaser.py`: the adapter, and the bridge with the helper over a real WebSocket in Node (Node 22 or
  later).
