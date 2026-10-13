import { defineConfig } from '@playwright/test';
import path from 'node:path';

const python = process.env.PHOTOSWEEP_TEST_PYTHON || (process.platform === 'win32' ? '../.venv/Scripts/python.exe' : '../.venv/bin/python');
export default defineConfig({
  testDir: './tests/e2e',
  workers: 1,
  retries: 0,
  timeout: 60000,
  use: { baseURL: 'http://127.0.0.1:8765', headless: true, trace: 'retain-on-failure', screenshot: 'only-on-failure' },
  webServer: {
    command: `"${path.resolve(python)}" ../scripts/e2e_server.py`,
    url: 'http://127.0.0.1:8765/health/ready',
    reuseExistingServer: false,
    timeout: 30000,
  },
});
