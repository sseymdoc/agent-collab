import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 开发时把 /api 代理到本机候选 controller（端口 18669，与生产 runner 隔离）。
export default defineConfig({
  plugins: [react()],
  base: '/',
  build: { outDir: '../agent_collab/static', emptyOutDir: true },
  server: { proxy: { '/api': { target: 'http://127.0.0.1:18669', changeOrigin: false } } },
})
