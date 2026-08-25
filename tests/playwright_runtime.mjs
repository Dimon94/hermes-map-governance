import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";


const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const packagePath = path.join(repositoryRoot, "package.json");
const installedPackagePath = path.join(
  repositoryRoot,
  "node_modules",
  "playwright",
  "package.json",
);
const recovery = "Run `npm ci && npm run playwright:install` from the repository root.";

function runtimeError(reason, cause) {
  const detail = cause instanceof Error ? ` Cause: ${cause.message}` : "";
  return new Error(
    `Dashboard Playwright runtime is unavailable: ${reason} ${recovery}${detail}`,
    { cause },
  );
}

export async function launchDashboardChromium() {
  let declaredVersion;
  let installedVersion;
  try {
    declaredVersion = JSON.parse(fs.readFileSync(packagePath, "utf8")).devDependencies.playwright;
    installedVersion = JSON.parse(fs.readFileSync(installedPackagePath, "utf8")).version;
  } catch (error) {
    throw runtimeError("the repository-owned module cannot be resolved.", error);
  }
  if (!/^\d+\.\d+\.\d+$/.test(declaredVersion) || installedVersion !== declaredVersion) {
    throw runtimeError(
      `expected pinned Playwright ${declaredVersion}, found ${installedVersion}.`,
    );
  }

  process.env.PLAYWRIGHT_BROWSERS_PATH = "0";
  let chromium;
  try {
    const require = createRequire(packagePath);
    ({ chromium } = require(path.dirname(installedPackagePath)));
  } catch (error) {
    throw runtimeError("the repository-owned module failed to load.", error);
  }
  try {
    return await chromium.launch({ headless: true });
  } catch (error) {
    throw runtimeError("the pinned Chromium binary failed to launch.", error);
  }
}
