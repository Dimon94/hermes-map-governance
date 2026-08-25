import { launchDashboardChromium } from "./playwright_runtime.mjs";


try {
  const browser = await launchDashboardChromium();
  await browser.close();
  process.stdout.write("dashboard Playwright preflight ready\n");
} catch (error) {
  process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
  process.exitCode = 1;
}
