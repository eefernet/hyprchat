import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import {readFileSync,writeFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {fileURLToPath} from 'node:url';

// HyprChat frontend build.
//   root      = frontend/ (this dir); entry is frontend/index.html
//   outDir    = frontend/dist/ — served verbatim by FastAPI StaticFiles(html=True)
//               in backend/main.py, so the built index.html + assets/ drop in
//               with no backend change.
//   base './' = assets are referenced relatively, so they resolve under the
//               app.mount("/") static mount.
export default defineConfig({
  base: './',
  plugins: [react(), {
    name: 'version-pwa-shell',
    apply: 'build',
    closeBundle() {
      const sw=fileURLToPath(new URL('./dist/sw.js',import.meta.url));
      const index=fileURLToPath(new URL('./dist/index.html',import.meta.url));
      const source=readFileSync(sw,'utf8');
      const version=createHash('sha256').update(readFileSync(index)).update(source).digest('hex').slice(0,16);
      writeFileSync(sw,source.replace('hyprchat-shell-v1',`hyprchat-shell-${version}`));
    },
  }],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    // The app is one large component file; keep Vite quiet about chunk size.
    chunkSizeWarningLimit: 4000,
  },
});
