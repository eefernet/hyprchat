import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// HyprChat frontend build.
//   root      = frontend/ (this dir); entry is frontend/index.html
//   outDir    = frontend/dist/ — served verbatim by FastAPI StaticFiles(html=True)
//               in backend/main.py, so the built index.html + assets/ drop in
//               with no backend change.
//   base './' = assets are referenced relatively, so they resolve under the
//               app.mount("/") static mount.
export default defineConfig({
  base: './',
  plugins: [react()],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    // The app is one large component file; keep Vite quiet about chunk size.
    chunkSizeWarningLimit: 4000,
  },
});
