import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 开发时把 /api 代理到本机候选 controller（端口 18669，与生产 runner 隔离）。
// controller 只接受空 Origin 或其 public_origin；开发服务器的 Origin 不同，代理时去掉（CSRF 令牌仍然必需）。
export default defineConfig({
  plugins: [react()],
  base: '/',
  // license：把打包进产物的依赖（React 等）的许可证文本写进产物，随发行包分发。
  build: { outDir: '../agent_collab/static', emptyOutDir: true, license: { fileName: 'third-party-licenses.md' } },
  server: { proxy: { '/api': { target: 'http://127.0.0.1:18669', changeOrigin: false,
    configure: proxy => { proxy.on('proxyReq', req => req.removeHeader('origin')) } } } },
})
