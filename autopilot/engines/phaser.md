
## The engine: Phaser (a 2D game in the web browser)

- **This is a 2D game.** Positions are in the kit's 2D frame, as a Godot 2D game's: kit X is screen right, kit Z is
  screen up, kit Y is 0, and 1 pixel is 1 kit unit (Phaser's x right, y down: `kit (X, Y, Z) = (x, 0, -y)`). A turn on
  screen is a pitch (`Pitch = -deg(rotation)`: clockwise is down); yaw is 0, or 180 for a direction to the left. The
  3D controllers that turn the view (`walk_to`, `look_at`, `forward`) don't apply: press the game's controls (or
  `game.request("move", ...)`) and read `game.loc` and `game.pose()`.
- **Things are the game objects of the Phaser scenes**, named by their paths: `/Game/<scene key>/<object>`, and a
  Container's objects under it (`/Game/Level/coins/coin#2`). An unnamed object is named by its type (`Sprite`,
  `Image`, `Text`, `Rectangle`...); a name repeated among siblings gets `#2`, `#3` in display order. A path names an
  object by its place: an object the game adds or removes can change its siblings' `#n`. Only the active scenes are
  read (the game may name fewer).
- **Classes**: `game.actors("Sprite")` (a Phaser type) or `game.actors("Coin")` (a class the game gives its objects:
  the Data Manager's `class`, `setData("class", "Coin")`, or the game's own `classOf` hook). `game.all_actors()`: each
  scene's own objects and the objects the game marks (`setData("thing", true)` or a class), not every object inside a
  Container. A class nothing in the scene is, is an error, not an empty list.
- **Screen-space objects** (a scroll factor of 0, or an object in another scene than the one with the world camera,
  such as a HUD scene) are placed where they show over the world, and `game.view` gives them `label`: a Text's text,
  or the Data Manager's `label`. Read the HUD there (`Score 10`).
- **Reads and calls** (`game.get`, `game.props`, `game.call`, `game.describe`) reach the game objects themselves by
  path (`game.get("/Game/Level/player", "x")`) and the objects the game names in its hooks (`objects`):
  `game.get("state", "score")`, a part with dots (`game.get("state", "combo.multiplier")`). `game.call(obj, fn,
  **params)` passes the parameters to the function as one object (`args=[...]` passes them in order); a function that
  returns a promise is waited for. A game object inside a value reads as its path. There is no reflection of the
  game's variables beyond these: read the codex to find what the game names, and how its state is shaped.
- **Values from `game.get` are the game's own**: Phaser pixels, y down, rotations in radians. `game.to_kit(value)` turns
  a position or velocity (`{"x","y"}` or `[x, y]`) into the kit's frame; `game.to_native(vec)` turns a kit point back
  to `{"x","y"}`. Everything else the kit gives (`game.loc`, `game.locs`, `game.bounds`, `game.pose`, `game.view`,
  `game.sees`, `movement`) is in the kit's frame already.
- **The player** is what the game's `player` hook says: `game.pawn` is its game object's path; `game.pose()["at"]` is
  the middle of its box, `velocity` and `on_ground` come from its Arcade body (or the hook), `yaw` and `pitch` are the
  camera's (0), and `eye` is the middle of the camera's view. `game.request("pose")` also gives `feet` (the bottom
  middle of its box), `body_yaw` (180 when it faces left: `flipX`) and the game's `state` for the player, when the
  hook gives one.
- **Input**: keys are Unreal names as everywhere (D, Right, SpaceBar, LeftShift, LeftMouseButton...); they reach the
  page as KeyboardEvents (`code` KeyD, ArrowRight, Space...), so Phaser's keyboard plugin sees them as a player's.
  Gamepad keys (`Gamepad_FaceButton_Bottom`, `Gamepad_LeftX`...) go to a virtual standard gamepad that
  `navigator.getGamepads()` lists first while it holds anything. The game's actions (controls.txt,
  `context()["controls"]`) can be pressed by name: `game.press("jump")` presses the action's first key. Phaser reads
  its keys once a frame: `game.press` holds a key two frames; to be sure of a move, `game.hold` it for the time the move
  takes, then `game.release`. `JustDown` sees a key once per press: release it before you press it again.
- **Pointing and clicking.** `game.pointer(...)` puts the pointer on the canvas (pointer and mouse events with no
  movement; `object=` a thing's path puts it on that thing); `game.press("LeftMouseButton")` then presses there, so
  Phaser's input plugin sees a click on the game object under it.
- `game.request("move", {"direction": kit vector, "seconds": s, "scale"?: -1..1})` goes that way the game's own way
  when it gives a `move` hook (`via` "game"), else with keys held relative to the screen (`via` "keys"): kit +X holds
  D, -X holds A, +Z holds W, -Z holds S (the game may name other keys, such as the arrows). Answers `{"moved", "via"}`.
  In a platformer, up is usually a jump: press it.
- **Levels**: `game.restart()` restarts the camera's scene (or calls the game's restart hook); `game.restart(map=...)`
  starts another scene by its key, as `game.request("levels")` names them (every scene of the game, not only its
  levels: read the codex for which are levels). Game time (`game.t`) is the sum of the game's frame times: it stops
  while the game is paused and goes on through a restart.
- **The UI**: Phaser draws its UI on the canvas, as game objects: read it with `game.view` (labels) and click it with
  `game.pointer(object=...)`. `game.screen()` and `game.ui()` list only the page's DOM over the canvas (`owner.id`
  keys), which a Phaser game may not have.
- **`game.view`** lists the things in the camera's view (a box of theirs on screen), nearest the camera's middle first,
  without the player: name, path, class, center, size, distance, screen_x/screen_y (-1..1, y up), top/bottom against
  the feet, `partly_in_view`, and `label`. `camera` gives the view's `location` (its middle), `width` and `height`. It
  does no line-of-sight test (`omitted` says what else it doesn't tell). A wide flat floor is left out.
- **Pictures**: `capture` and `record` read the canvas. A WebGL game made without `preserveDrawingBuffer` is copied 10
  times a second after a frame is drawn (`postrender`), so a picture is at most 0.1 s old.
- **Time**: the frame rate is the display's; `timestep` changes nothing. `pause` pauses the whole game
  (`game.pause()`), or calls the game's pause hook.
