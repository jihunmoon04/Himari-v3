// Browser checks of the built page against a real HIMARI server with a scripted model (scripts/web_next_server.py).
// CI runs the server here; elsewhere start it yourself (e.g. in WSL) and set HIMARI_E2E_EXTERNAL=1.
import { defineConfig, devices } from "@playwright/test";

const port = Number(process.env.HIMARI_E2E_PORT ?? 8798);
export const tokenFile = process.env.HIMARI_E2E_TOKEN ?? ".e2e-token";

export default defineConfig({
  testDir: "e2e",
  timeout: 30_000,
  expect: { timeout: 10_000 },
  workers: 1, // one server, one scripted model: the checks run in order
  fullyParallel: false,
  reporter: process.env.CI ? "list" : [["list"]],
  use: { baseURL: `http://127.0.0.1:${port}`, trace: "retain-on-failure" },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"] } },
    { name: "phone", use: { ...devices["Pixel 7"] } },
  ],
  webServer: process.env.HIMARI_E2E_EXTERNAL
    ? undefined
    : {
        command: `python -I ../scripts/web_next_server.py --port ${port} --token-out ${tokenFile}`,
        url: `http://127.0.0.1:${port}/api/session`,
        reuseExistingServer: false,
        timeout: 60_000,
      },
});
