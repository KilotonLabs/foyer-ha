// Foyer renderer: keeps Home Assistant dashboard views open in a headless browser and
// returns pictures of them. Roku has no web engine, so custom cards can only reach the TV this way.
// The Foyer integration is the only intended client; TVs never talk to this service directly.
import { createServer } from "node:http";
import { existsSync, readFileSync } from "node:fs";
import { timingSafeEqual } from "node:crypto";
import { hostname } from "node:os";
import puppeteer from "puppeteer-core";

const options = existsSync("/data/options.json") ? JSON.parse(readFileSync("/data/options.json", "utf8")) : {};
const config = {
  haUrl: (process.env.HA_URL || options.ha_url || "http://homeassistant:8123").replace(/\/+$/, ""),
  token: process.env.HA_TOKEN || options.ha_token || "",
  secret: process.env.RENDERER_SECRET || options.secret || "",
  port: Number(process.env.PORT || 8099),
  host: process.env.HOST || "0.0.0.0",
  dark: (process.env.DARK ?? String(options.dark ?? true)) !== "false",
  width: Number(process.env.WIDTH || 1920),
  height: Number(process.env.HEIGHT || 1080),
  quality: Number(process.env.JPEG_QUALITY || 80),
  // A TV is read from across the room: lay the page out smaller and render it larger so text stays legible.
  scale: Math.min(3, Math.max(1, Number(process.env.SCALE || options.scale || 1.5))),
  browserPath: process.env.BROWSER_PATH || [
    "/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome",
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  ].find((path) => existsSync(path)),
};
const haOrigin = (() => {
  try {
    const url = new URL(config.haUrl);
    return ["http:", "https:"].includes(url.protocol) ? url.origin : "";
  } catch {
    return "";
  }
})();
const PATH_RE = /^\/[A-Za-z0-9_-]{1,64}(\/[A-Za-z0-9_-]{1,64})?$/;
const IDLE_MS = 120_000;
const MAX_PAGES = 4;
const FRESH_MS = 1000;

// Anything on the network that can reach this port could otherwise screenshot dashboards.
const problem = !haOrigin ? "Set ha_url to Home Assistant's address, such as http://homeassistant:8123."
  : !config.token ? "Set ha_token (a long-lived access token for a non-admin HA user)."
  : config.secret.length < 16 ? "Set secret (16 or more characters) and enter the same secret in Foyer settings."
  : !config.browserPath ? "No Chromium found; set BROWSER_PATH." : "";
if (problem) {
  console.error(problem);
  process.exit(1);
}
const firstLine = (error) => String(error?.message ?? error).split("\n")[0];
process.on("unhandledRejection", (error) => console.error(`Unhandled renderer error: ${firstLine(error)}`));

let launching; // Promise of the one shared browser.
const views = new Map(); // path -> { ready, queue, used, frame, at }

function getBrowser() {
  launching ??= launch().catch((error) => {
    launching = undefined;
    throw error;
  });
  return launching;
}

async function launch() {
  // Every open view must keep painting even when it is not the front tab, or screenshots stall.
  const args = ["--disable-dev-shm-usage", "--hide-scrollbars", "--mute-audio", "--no-first-run", "--disable-gpu",
    "--disable-background-timer-throttling", "--disable-renderer-backgrounding", "--disable-backgrounding-occluded-windows"];
  if (process.getuid?.() === 0) args.push("--no-sandbox"); // Container root; the page only ever loads HA.
  // A hung page must fail fast so it can be reopened, rather than after the default three minutes.
  const browser = await puppeteer.launch({ executablePath: config.browserPath, headless: true, args, protocolTimeout: 25_000 });
  browser.on("disconnected", () => {
    launching = undefined;
    views.clear();
  });
  return browser;
}

// Hide HA's toolbar and sidebar so the view fills the TV. Returns true once a view is on screen.
function kiosk() {
  const inner = (node, selector) => node && (node.shadowRoot || node).querySelector(selector);
  const main = inner(document.querySelector("home-assistant"), "home-assistant-main");
  const panel = inner(inner(main, "partial-panel-resolver"), "ha-panel-lovelace");
  const root = inner(panel, "hui-root");
  if (!root?.shadowRoot) return false;
  if (!root.shadowRoot.getElementById("foyer-kiosk")) {
    const style = document.createElement("style");
    style.id = "foyer-kiosk";
    style.textContent = ".header, .toolbar { display: none !important; }"
      + " #view { padding-top: 0 !important; min-height: 100vh !important; }"
      + " :host { --header-height: 0px !important; }";
    root.shadowRoot.appendChild(style);
  }
  return Boolean(root.shadowRoot.querySelector("hui-view, hui-panel-view, hui-sections-view, hui-masonry-view"));
}

async function open(path) {
  const page = await (await getBrowser()).newPage();
  try {
    await page.setViewport({ width: Math.round(config.width / config.scale), height: Math.round(config.height / config.scale), deviceScaleFactor: config.scale });
    const tokens = JSON.stringify({ access_token: config.token, token_type: "Bearer", expires_in: 1800,
      hassUrl: config.haUrl, clientId: config.haUrl + "/", expires: Date.now() + 10 * 365 * 86400_000, refresh_token: "" });
    // This runs in every frame, including Webpage cards from other sites: only HA's own top-level page gets the token.
    await page.evaluateOnNewDocument((tokens, dark, origin) => {
      if (window !== window.top || location.origin !== origin) return;
      localStorage.setItem("hassTokens", tokens);
      localStorage.setItem("dockedSidebar", JSON.stringify("always_hidden"));
      localStorage.setItem("selectedTheme", JSON.stringify({ theme: "default", dark }));
    }, tokens, config.dark, haOrigin);
    await page.goto(config.haUrl + path, { waitUntil: "domcontentloaded", timeout: 20_000 });
    await page.waitForFunction(kiosk, { timeout: 20_000, polling: 250 });
    await new Promise((done) => setTimeout(done, 1500)); // Let cards finish their first render.
    return page;
  } catch (error) {
    await page.close().catch(() => {});
    throw error;
  }
}

function view(path) {
  let entry = views.get(path);
  if (!entry) {
    if (views.size >= MAX_PAGES) close(...[...views.entries()].sort((a, b) => a[1].used - b[1].used)[0]);
    const created = { ready: open(path), queue: Promise.resolve(), used: Date.now(), frame: null, at: 0 };
    created.ready.catch(() => close(path, created));
    views.set(path, created);
    entry = created;
  }
  entry.used = Date.now();
  return entry;
}

// Close one specific entry; a newer entry already reopened at the same path is left alone.
function close(path, entry = views.get(path)) {
  if (!entry) return;
  if (views.get(path) === entry) views.delete(path);
  entry.ready.then((page) => page.close().catch(() => {}), () => {});
}

async function frame(entry) {
  const page = await entry.ready;
  // Several TVs on one view share a picture; screenshots of a page never overlap.
  const shot = entry.queue.then(async () => {
    if (Date.now() - entry.at < FRESH_MS) return;
    await page.bringToFront();
    await page.evaluate(kiosk);
    entry.frame = await page.screenshot({ type: "jpeg", quality: config.quality });
    entry.at = Date.now();
  });
  entry.queue = shot.catch(() => {});
  await shot;
  return entry.frame;
}

function authorized(request) {
  const given = Buffer.from(String(request.headers["x-foyer-secret"] || ""));
  const expected = Buffer.from(config.secret);
  return given.length === expected.length && timingSafeEqual(given, expected);
}

const server = createServer(async (request, response) => {
  const send = (status, body, type = "application/json") => {
    if (response.headersSent) return;
    response.writeHead(status, { "Content-Type": type, "Cache-Control": "no-store" });
    response.end(body);
  };
  let path = "";
  let entry;
  try {
    const url = new URL(request.url, "http://renderer");
    if (request.method !== "GET") return send(405, '{"error":"method"}');
    if (url.pathname === "/health") return send(200, '{"ok":true}');
    if (url.pathname !== "/frame") return send(404, '{"error":"not_found"}');
    if (!authorized(request)) return send(401, '{"error":"secret"}');
    const requested = url.searchParams.get("path") || "";
    if (!PATH_RE.test(requested)) return send(400, '{"error":"path"}');
    path = requested;
    entry = view(path);
    send(200, await frame(entry), "image/jpeg");
  } catch (error) {
    if (!path) return send(400, '{"error":"bad_request"}');
    console.error(`Could not render ${path}: ${firstLine(error)}`);
    close(path, entry);
    send(502, '{"error":"render_failed"}');
  }
});

setInterval(() => {
  for (const [path, entry] of views) if (Date.now() - entry.used > IDLE_MS) close(path, entry);
}, 30_000).unref();

server.listen(config.port, config.host, () => {
  console.log(`Foyer renderer on port ${config.port}, rendering ${config.haUrl} with ${config.browserPath}.`);
  const where = existsSync("/data/options.json") ? hostname() : "<this machine's address>"; // Add-ons share HA's network.
  console.log(`In Foyer settings, set the renderer URL to http://${where}:${config.port} and the same secret.`);
});
for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, async () => {
    await launching?.then((browser) => browser.close()).catch(() => {});
    process.exit(0);
  });
}
