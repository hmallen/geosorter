import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The build output goes straight into the Python package's SPA mount point
// (api.create_app serves src/geosorter/webui when it exists). The dev server
// proxies /api to the running `geosorter serve` backend. Vitest config lives in
// vitest.config.ts to avoid a vite/vitest plugin-type version clash.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: '../src/geosorter/webui',
    emptyOutDir: true,
  },
  server: {
    proxy: { '/api': 'http://127.0.0.1:8000' },
  },
})
