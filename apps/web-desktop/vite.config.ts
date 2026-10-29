/// <reference types="vitest/config" />
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  resolve: {
    // The design system is a symlinked package with its own node_modules; keep a single React copy.
    dedupe: ['react', 'react-dom'],
  },
  server: {
    port: 5173,
    // Loopback by default; set VITE_DEV_HOST (e.g. the Tailscale IP) to expose it on the mesh only.
    host: process.env.VITE_DEV_HOST ?? '127.0.0.1',
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:9870',
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: 'jsdom',
    include: ['tests/**/*.test.{ts,tsx}'],
    setupFiles: ['tests/setup.ts'],
    css: false,
    restoreMocks: true,
  },
});
