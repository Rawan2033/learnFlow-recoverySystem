import {defineConfig} from '@playwright/test';
export default defineConfig({testDir:'./tests', fullyParallel:false, workers:1,
  use:{baseURL:'http://127.0.0.1:5173', browserName:'chromium', channel:process.platform==='win32'?'msedge':undefined, headless:true, viewport:{width:1440,height:1050}},
  webServer:{command:'npm run dev', url:'http://127.0.0.1:5173', reuseExistingServer:false, timeout:30000},
  reporter:'list'});
