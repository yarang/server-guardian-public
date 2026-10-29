import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

// Library-mode JS build: ESM only, React kept external. The JS never imports CSS.
// The stylesheet is built separately by vite.styles.config.ts, because Vite library
// mode inlines every asset (it would embed the fonts as base64 in the CSS).
export default defineConfig({
  plugins: [react()],
  build: {
    target: 'es2020',
    sourcemap: false,
    lib: {
      entry: {
        index: 'src/index.ts',
        mount: 'src/mount.tsx',
      },
      formats: ['es'],
      fileName: (_format, name) => `${name}.js`,
    },
    rollupOptions: {
      external: [/^react(\/.*)?$/, /^react-dom(\/.*)?$/],
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./tests/setup.ts'],
    include: ['tests/**/*.test.{ts,tsx}'],
    coverage: {
      provider: 'v8',
      include: ['src/**/*.{ts,tsx}'],
      reporter: ['text-summary', 'text'],
    },
  },
});
