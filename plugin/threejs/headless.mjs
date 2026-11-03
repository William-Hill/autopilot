// Open a Three.js game's page in headless Chromium with Autopilot's bridge turned on, for a headless run
// (autopilot/engines/threejs.py's launch_command, bench/run_autopilot.py):
//
//   node headless.mjs --project <folder> --page http://localhost:5173/ --bridge ws://127.0.0.1:8765/v1/bridge \
//     --log game.log [--level <name>]
//
// The page must be served already (the game's dev server). The page gets ?autopilot=1&autopilot_url=<bridge>
// (&level=<name>): the game imports its bridge when it sees them. Playwright comes from the project's own
// node_modules (this file adds no package). The browser stays open until the page closes or this process is stopped.

import { appendFileSync } from "node:fs";
import { createRequire } from "node:module";
import { join, resolve } from "node:path";

function args(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i += 1) if (argv[i].startsWith("--")) out[argv[i].slice(2)] = argv[i + 1], (i += 1);
  return out;
}

const a = args(process.argv.slice(2));
if (!a.page || !a.bridge) {
  console.error("headless.mjs needs --page and --bridge");
  process.exit(2);
}
const log = (line) => (a.log ? appendFileSync(a.log, `${line}\n`) : console.log(line));
const require = createRequire(join(resolve(a.project ?? "."), "package.json"));
let playwright;
try {
  playwright = require("@playwright/test");
} catch {
  playwright = require("playwright");
}

const url = new URL(a.page);
url.searchParams.set("autopilot", "1");
url.searchParams.set("autopilot_url", a.bridge);
if (a.level) url.searchParams.set("level", a.level);

const browser = await playwright.chromium.launch({
  headless: true,
  args: ["--enable-unsafe-swiftshader", "--autoplay-policy=no-user-gesture-required"],
});
const page = await browser.newPage({ viewport: { width: 1280, height: 720 } });
page.on("console", (m) => log(`[${m.type()}] ${m.text()}`));
page.on("pageerror", (e) => log(`[pageerror] ${e.message}`));
await page.goto(url.toString());
log(`opened ${url}`);
const stop = async () => {
  await browser.close().catch(() => {});
  process.exit(0);
};
process.on("SIGTERM", stop);
process.on("SIGINT", stop);
page.on("close", stop);
