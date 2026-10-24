// Real Home Assistant runtime tests. HA needs Linux, so on Windows they run in WSL.
import { spawnSync } from "node:child_process";
import { resolve } from "node:path";
const root = resolve(import.meta.dirname, "..");
const args = ["-m", "pytest", "-q", "-p", "no:sugar", "-p", "no:cacheprovider", ...process.argv.slice(2)];
let result;
if (process.platform === "win32") {
  const linuxRoot = "/mnt/" + root[0].toLowerCase() + root.slice(2).replaceAll("\\", "/");
  const quoted = args.map((a) => `'${a}'`).join(" ");
  result = spawnSync("wsl.exe", ["-d", process.env.HC_WSL_DISTRO ?? "Ubuntu-24.04", "-e", "bash", "-lc",
    `cd '${linuxRoot}' && ~/hc-ha-venv/bin/python ${quoted}`], { stdio: "inherit" });
} else {
  result = spawnSync(resolve(root, ".venv-ha/bin/python"), args, { cwd: root, stdio: "inherit" });
}
if (result.error) console.error(result.error.message);
process.exit(result.status ?? 1);
