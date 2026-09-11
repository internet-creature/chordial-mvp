import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  use: {
    baseURL: "http://localhost:1422",
    viewport: { width: 1100, height: 720 },
  },
  webServer: {
    command: "npm run dev -- --port 1422",
    url: "http://localhost:1422",
    reuseExistingServer: false,
  },
});
