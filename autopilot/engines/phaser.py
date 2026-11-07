"""Phaser: a 2D game in the browser, with the browser bridge (plugin/threejs/autopilot-bridge.js) and the Phaser helper
(plugin/phaser/autopilot-phaser.js) imported by its page. Phaser has no 3D scene graph: the helper builds one of the
shape the bridge reads from the Phaser scenes' game objects, and the bridge answers the engine-neutral ops in 2D.
Things are the game objects, by path (/Game/<scene key>/<object>/...).

Phaser's frame: x right, y down, in pixels; rotations in radians, clockwise on screen. The kit's frame for a 2D game is
the Godot plugin's: kit X is screen right, kit Z is screen up, kit Y is 0, and 1 pixel is 1 kit unit:
    kit (X, Y, Z) = (x, 0, -y);  kit pitch = -deg(rotation);  kit yaw = 0

The project on disk, the codex and the headless launch are the Three.js adapter's (a web project: package.json, the
source text, plugin/threejs/headless.mjs).
"""

import math

from ..kit.geom import Vec, wrap
from .threejs import ThreeJS


class Phaser(ThreeJS):
    name = "phaser"
    title = "Phaser (a 2D game in the web browser)"
    package = "phaser"
    things_note = ("- Things are the Phaser scenes' game objects, by path (/Game/<scene key>/<object>/...; a "
                   "Container's objects under it): an unnamed object is named by its type, a repeated name gets #2, "
                   "#3. Positions are in the kit's 2D frame: X = x, Z = -y, 1 pixel = 1 unit.")

    # -- the kit frame (2D) ------------------------------------------------------------------------------

    def to_kit(self, v):
        """A Phaser position or velocity ({"x","y"} or [x, y] in pixels, y down, as game.get reads it) in the kit's 2D
        frame; None stays None."""
        if v is None:
            return None
        if isinstance(v, dict):
            return Vec(float(v.get("x", 0.0)), 0.0, -float(v.get("y", 0.0)))
        n = Vec.of(v)
        return Vec(n.x, 0.0, -n.y)

    def to_native(self, v):
        k = Vec.of(v)
        return {"x": k.x, "y": -k.z}

    def rotation_to_kit(self, r):
        """A Phaser rotation (radians, clockwise on screen: a number, or {"rotation"} or {"z"}) as (yaw, pitch) in
        degrees: a 2D turn is a kit pitch."""
        if isinstance(r, dict):
            r = r.get("rotation", r.get("z", 0.0))
        return 0.0, wrap(-math.degrees(float(r or 0.0)))

    def rotation_to_native(self, yaw, pitch=0.0):
        return {"rotation": -math.radians(wrap(pitch))}
