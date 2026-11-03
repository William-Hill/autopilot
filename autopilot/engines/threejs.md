
## The engine: Three.js (a game in the web browser)

- **Things are the scene graph's objects**, named by their paths from the scene: `/Scene/Level/Rail`, `/Scene/Player`.
  An unnamed object is named by its type (`/Scene/Group/Mesh`); a name repeated among siblings gets `#2`, `#3` in their
  order (`/Scene/Level/Cone#3`). A "component" is a child object. `game.name_of(path)` is the last part. A path names an
  object by its place: an object the game adds or removes can change its siblings' `#n`.
- **Classes**: `game.actors("Mesh")` (a Three.js type, with what it inherits: `Mesh` also finds `SkinnedMesh`) or
  `game.actors("Rail")` (a class the game gives its objects: `userData.class`, or the game's own `classOf` hook).
  `game.all_actors()`: the scene's things (its direct children and the objects the game marks with `userData.thing` or
  a class), not every mesh under them; `game.named("rail")` searches those. A class nothing in the scene is, is an
  error, not an empty list.
- **Reads and calls** (`game.get`, `game.props`, `game.call`, `game.describe`) reach the objects the game names in its
  hooks (`objects`): `game.get("state", "score")`, a part with dots (`game.get("state", "combo.multiplier")`), a
  scene object by its path. `game.call(obj, fn, **params)` passes the parameters to the function as one object
  (`args=[...]` passes them in order); a function that returns a promise is waited for. There is no reflection of the
  game's variables beyond these: read the codex to find what the game names, and how its state is shaped.
- **Values from `game.get` are the game's own**: Three.js axes (x right, y up, -z forward), the game's unit (metres for
  most games), vectors as `{"x","y","z"}` or `[x, y, z]`, an object as its path, rotations in radians.
  `game.to_kit(value)` turns a position or velocity into the kit's frame (`kit = 100 * (-z, x, y)` for metres);
  `game.to_native(vec)` turns a kit point back. Everything else the kit gives (`game.loc`, `game.locs`, `game.bounds`,
  `game.pose`, `game.view`, `game.sees`, `movement`) is in the kit's frame already.
- **The player** is what the game's `player` hook says: `game.pawn` is its object's path; `game.pose()["at"]` is the
  middle of its body (`feet` plus its half height), `on_ground` and `velocity` come from the game's own state, `yaw`
  and `pitch` are the camera's. `body_yaw` (when the game gives it) is the way the body faces, which a game with a
  following camera turns toward over time. `game.controller` is "". The game's state for the player may say more
  (`pose()`'s `state`, as the hook gives it: e.g. a glider's "diving").
- **Input**: keys are Unreal names as everywhere (W, SpaceBar, E, LeftShift, LeftMouseButton, MouseX...); they reach the
  page as KeyboardEvents (`code` KeyW, Space, ShiftLeft...), so a game that listens for keydown/keyup sees them as a
  player's. Gamepad keys (`Gamepad_FaceButton_Bottom`, `Gamepad_LeftX`, `Gamepad_RightTrigger`...) go to a virtual
  standard gamepad that `navigator.getGamepads()` lists first while it holds anything, so a game that polls the Gamepad
  API sees them. The game's actions (controls.txt, `context()["controls"]`, with their keys) can be pressed by name:
  `game.press("jump")` presses the action's first key. A game that samples its input once a fixed tick needs a key
  down for at least one tick: `game.press` holds it two frames; to be sure of a move, `game.hold` it for the time the
  move takes, then `game.release`.
- **Pointing and clicking.** `game.pointer(...)` puts the pointer on the canvas (pointer and mouse events with no
  movement); `game.press("LeftMouseButton")` then presses there (pointerdown, mousedown, then up and click).
  `game.mouse` / `game.turn` send pointermove/mousemove with `movementX`/`movementY`: the pointer is never locked, so a
  game that turns the view only under pointer lock won't turn: read its code for that, and call it out.
- `game.request("move", {"direction": kit vector, "seconds": s, "scale"?: -1..1})` goes that way the game's own way
  when it gives a `move` hook (`via` "game": a vehicle steers toward it), else with W/A/S/D held relative to
  the camera (`via` "keys", 8 directions). Answers `{"moved", "via"}`. A body that turns and rolls rather than walks
  (it keeps its speed, it steers) reaches a point late and wide: read `pose()` as it goes.
- **Levels**: `game.restart()` calls the game's restart hook (the level starts over, the player at its start);
  `game.restart(map=...)` asks it for another level, as `game.request("levels")` names them. A game without the hook
  answers 501. Game time (`game.t`) is the game's own clock when it gives one, else the page's.
- **The UI is the page's DOM over the canvas**: `game.screen()` keys are `<owner>.<id or class>` (`hud.hud-score`), the
  owner the nearest element with an id; `game.ui()` lists buttons, links, inputs, selects and sliders; `ui_act` clicks
  one (`element.click()`) or sets its value.
- **`game.view`** lists the things in the camera's view (a box of theirs on screen), nearest first, without the player:
  name, path, class, center, size, distance, screen_x/screen_y, `crosshair_on`, top/bottom against the feet,
  `partly_in_view`, and `label` (`userData.label`). It does no line-of-sight test: a thing behind a wall is listed
  (`omitted` says what else it doesn't tell: openings, ways_up, sonar, look). A wide flat floor is left out.
- **Pictures**: `capture` and `record` read the canvas after the game's `render` hook draws a frame. Without the hook a
  WebGL canvas made without `preserveDrawingBuffer` can read back black.
- **Time**: the frame rate is the display's; `timestep` changes nothing. `pause` calls the game's pause hook.
