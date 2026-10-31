import {defineConfig} from '@playwright/test';
const port=Number(process.env.LEARNFLOW_BROWSER_TEST_PORT||5173);
export default defineConfig({testDir:'./tests', fullyParallel:false, workers:1,
  use:{baseURL:`http://127.0.0.1:${port}`, browserName:'chromium', channel:process.platform==='win32'?'msedge':undefined, headless:true, viewport:{width:1440,height:1050}},
  webServer:{command:`npm run dev -- --port ${port}`, url:`http://127.0.0.1:${port}`, reuseExistingServer:false, timeout:30000},
  reporter:'list'});
