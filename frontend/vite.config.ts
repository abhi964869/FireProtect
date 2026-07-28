// `defineConfig` comes from vitest/config, not vite, so the `test` block below
// is type-checked rather than silently accepted as an unknown key.
import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import path from 'node:path';

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { '@': path.resolve(__dirname, './src') },
  },
  server: {
    port: 5173,
    // The backend runs on 8000. Proxying keeps the browser same-origin, so
    // the dashboard works without CORS in dev and the WebSocket URL is
    // derived from window.location rather than hard-coded.
    proxy: {
      '/api': { target: 'http://localhost:8000', changeOrigin: true },
      '/ws': { target: 'ws://localhost:8000', ws: true },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    /*
     * Recharts alone is ~525 kB minified (~150 kB gzipped) and cannot be split
     * further - it is one vendor library. It is already loaded lazily (see the
     * `lazy(() => import(...))` in App.tsx), so it never blocks first paint,
     * and the default 500 kB warning threshold would flag it on every build
     * with no action available. Raised just past it rather than left to cry
     * wolf on every CI run.
     */
    chunkSizeWarningLimit: 600,
    /*
     * Stable, unhashed output filenames.
     *
     * Vite's default `index-a1b2c3d4.js` is excellent when a build pipeline
     * publishes the whole `dist` atomically. This project's dist is committed
     * and uploaded by hand, and that turns content hashes into a foot-gun: the
     * hash changes on every build, so each deploy adds *new* filenames, and if
     * a single one fails to upload, `index.html` asks for a file that does not
     * exist and the page renders blank white with no clue why. That has now
     * happened twice.
     *
     * Fixed names make an upload idempotent - the same six paths every time.
     * A file that fails to upload leaves the previous version in place, which
     * degrades to "slightly stale" instead of "completely broken".
     *
     * The trade-off is cache-busting: browsers may hold a stale `app.js`.
     * Handled by serving these with a short max-age rather than the usual
     * immutable year (see vercel.json), which is the right call when the
     * alternative failure is a blank site.
     */
    rollupOptions: {
      output: {
        entryFileNames: 'assets/app.js',
        chunkFileNames: 'assets/[name].js',
        assetFileNames: 'assets/app[extname]',
      },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    css: false,
  },
});
