import { existsSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { resolve, dirname } from "node:path";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const python = resolve(root, process.platform === "win32" ? ".venv/Scripts/python.exe" : ".venv/bin/python");
if (!existsSync(python)) {
  console.error("Create .venv and install requirements-dev.txt first. See README.md.");
  process.exit(1);
}
const result = spawnSync(python, process.argv.slice(2), { cwd: root, stdio: "inherit" });
if (result.error) console.error(result.error.message);
process.exit(result.status ?? 1);
