"""Three.js: a game in the browser, with the bridge module (plugin/threejs/autopilot-bridge.js) imported by its page.
The bridge answers the engine-neutral ops (find, transforms, pose, movement) in the kit frame, and `rc` on the objects
the game names in its hooks (dotted paths: `state.score`). Things are the scene graph's objects, by path
(/Scene/Level/Rail#2).

Three.js' frame: x right, y up, -z forward (a camera looks down -z), right-handed, in the game's own unit (metres for
most games; the bridge's `metres` option says otherwise); rotations in radians. The kit's: X forward, Y right, Z up,
centimetres. With metres, as Godot's:
    kit (X, Y, Z) = 100 * (-z, x, y);  kit yaw = -deg(rotation.y);  kit pitch = deg(rotation.x)

The codex is the project's own source text (JavaScript, TypeScript, shaders, HTML, CSS, JSON data under 512 KB),
copied from this machine's disk: the bridge has no codex ops (a page can't read its project's files).

A headless game: plugin/threejs/headless.mjs opens the game's page (AUTOPILOT_WEB_URL: a dev server already running)
in headless Chromium with ?autopilot=1, through the Playwright the project itself has.
"""

import json
import math
import os
import re
import shutil

from ..kit.geom import Vec, wrap
from .base import CODEX_OWN, OpEngine, safe_rel

HERE = os.path.dirname(os.path.abspath(__file__))
# The headless page opener (plugin/threejs/headless.mjs in a clone)
HEADLESS = os.path.normpath(os.path.join(HERE, "..", "..", "plugin", "threejs", "headless.mjs"))
NODE = os.environ.get("AUTOPILOT_NODE") or shutil.which("node") or "node"
# The game's page, served by its dev server (e.g. `npm run dev`)
WEB_URL = os.environ.get("AUTOPILOT_WEB_URL", "http://localhost:5173/")
CODE_EXT = (".js", ".mjs", ".cjs", ".ts", ".mts", ".tsx", ".jsx", ".vue", ".svelte", ".html", ".css", ".glsl", ".vert",
            ".frag", ".wgsl", ".json")
TOP_FILES = ("package.json", "index.html", "vite.config.js", "vite.config.ts", "vite.config.mjs", "tsconfig.json")
SKIP_DIRS = ("node_modules", "dist", "build", "coverage", "out", "public", "test-results", "playwright-report")
SKIP_FILES = ("package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lock", "autopilot-bridge.js")
MAX_BYTES = 512 * 1024  # a larger file (a baked level, a minified library) stays out of the codex


def read_package(folder):
    """package.json of a folder as a dict ({} when there is none or it isn't JSON)."""
    try:
        with open(os.path.join(folder, "package.json"), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def dep_version(package, name):
    """The version of a package (`name`) a package.json asks for, or None when it doesn't use it."""
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        v = (package.get(key) or {}).get(name)
        if v:
            return str(v)
    return None


def three_version(package):
    """The version of three a package.json asks for, or None when it doesn't use three."""
    return dep_version(package, "three")


class ThreeJS(OpEngine):
    name = "threejs"
    title = "Three.js (a game in the web browser)"
    export_dir = None  # (no export: the codex is the source text)
    plugin_files = ()
    default_exe = NODE
    package = "three"  # the npm package a project of this engine depends on
    # The codex index's line on how things are named
    things_note = ("- Things are the scene graph's objects, by path (/Scene/<child>/...): an unnamed object is named "
                   "by its type, a repeated name gets #2, #3.")

    def version_of(self, package):
        """The version of this engine's npm package a package.json (a dict) asks for, or None."""
        return dep_version(package, self.package)

    # -- the project on disk -----------------------------------------------------------------------------

    def project_at(self, path):
        """The project folder: one whose package.json depends on three (the folder itself, or the folder of that
        file)."""
        if not path:
            return None
        path = os.path.abspath(path)
        if os.path.isfile(path):
            if os.path.basename(path) != "package.json":
                return None
            path = os.path.dirname(path)
        if not os.path.isdir(path):
            return None
        return path if self.version_of(read_package(path)) else None

    def project_name(self, project):
        """package.json's name, else the folder's name."""
        name = read_package(self.project_folder(project)).get("name")
        return str(name) if isinstance(name, str) and name.strip() else super().project_name(project)

    # -- the game backend --------------------------------------------------------------------------------

    def all_actors(self, game):
        """The scene's things, as Unreal's actors: its direct children and the objects the game marks as things
        (userData.thing, or a class of its own), not every mesh under them."""
        return list((game.request("find", {"things": True}) or {}).get("objects") or [])

    # -- the codex ---------------------------------------------------------------------------------------

    def codex_path(self, rel):
        """Kept at their project paths: package.json, index.html, the bundler's config, and **/*.js, .ts, .jsx, .tsx,
        shaders, .html, .css, .json outside node_modules, the build's output and folders whose name starts with ".";
        never a lock file or the bridge itself."""
        parts = safe_rel(rel)
        if parts is None:
            return None
        if any(p.startswith(".") or p in SKIP_DIRS for p in parts[:-1]):
            return None
        name = parts[-1]
        if name in SKIP_FILES or name.endswith((".min.js", ".map")) or name in CODEX_OWN:
            return None
        if len(parts) == 1 and name in TOP_FILES:
            return rel
        if name.lower().endswith(CODE_EXT):
            return rel
        return None

    def skip_dir(self, rel):
        name = rel.split("/")[-1]
        return name.startswith(".") or name in SKIP_DIRS

    def walk_project(self, folder):
        """As Engine's, without files over MAX_BYTES."""
        for rel in super().walk_project(folder):
            try:
                if os.path.getsize(os.path.join(folder, *rel.split("/"))) > MAX_BYTES:
                    continue
            except OSError:
                continue
            yield rel

    def index_notes(self, codex_root):
        """Where to start in a web project: the page's entry scripts, the npm scripts, the engine's version, and
        where the game gives the bridge its hooks."""
        pkg = read_package(codex_root)
        lines = ["", "## Where to start (from package.json and index.html)", ""]
        version = self.version_of(pkg)
        if version:
            lines.append(f"- {self.package} {version}")
        scripts = pkg.get("scripts") or {}
        if scripts:
            lines.append("- npm scripts: " + ", ".join(f"{k} (`{v}`)" for k, v in scripts.items()))
        try:
            with open(os.path.join(codex_root, "index.html"), encoding="utf-8") as f:
                html = f.read()
            for src in re.findall(r"<script[^>]*\bsrc=[\"']([^\"']+)[\"']", html):
                lines.append(f"- The page loads {src} (in the codex as {src.lstrip('/')}): the game starts there")
        except OSError:
            pass
        hooks = []
        for base, _, files in os.walk(codex_root):
            for f in files:
                if not f.endswith((".js", ".mjs", ".ts", ".jsx", ".tsx")):
                    continue
                full = os.path.join(base, f)
                try:
                    with open(full, encoding="utf-8") as fh:
                        if "connectBridge" in fh.read():
                            hooks.append(os.path.relpath(full, codex_root).replace(os.sep, "/"))
                except OSError:
                    continue
        if hooks:
            lines.append("- The game gives the bridge its player, movement, controls and restart in: "
                         + ", ".join(sorted(hooks)) + " (read it to see what pose, movement and move mean here)")
        lines.append(self.things_note)
        return lines

    # -- the kit frame -----------------------------------------------------------------------------------

    def to_kit(self, v):
        if isinstance(v, (list, tuple)) and len(v) == 3:
            v = {"x": v[0], "y": v[1], "z": v[2]}
        n = Vec.of(v)
        return None if n is None else Vec(-100.0 * n.z, 100.0 * n.x, 100.0 * n.y)

    def to_native(self, v):
        k = Vec.of(v)
        return {"x": k.y / 100.0, "y": k.z / 100.0, "z": -k.x / 100.0}

    def rotation_to_kit(self, r):
        """rotation {"x","y","z"} (an Euler, radians) as (yaw, pitch) in degrees."""
        r = r or {}
        return wrap(-math.degrees(float(r.get("y", 0.0)))), wrap(math.degrees(float(r.get("x", 0.0))))

    def rotation_to_native(self, yaw, pitch=0.0):
        return {"x": math.radians(pitch), "y": -math.radians(wrap(yaw)), "z": 0.0}

    # -- a headless game ---------------------------------------------------------------------------------

    def launch_command(self, project, url, log, map=None, exe=None):
        """Node running plugin/threejs/headless.mjs in the project's folder (it loads the project's own Playwright):
        the game's page (AUTOPILOT_WEB_URL, or exe when it is a URL) opened headless with ?autopilot=1 and the brain's
        address; map: the level to start in (the page's ?level=)."""
        page = exe if exe and re.match(r"^https?://", exe) else WEB_URL
        node = exe if exe and not re.match(r"^https?://", exe) else NODE
        return [node, HEADLESS, "--project", self.project_folder(project), "--page", page, "--bridge", url,
                "--log", log] + (["--level", map] if map else [])
