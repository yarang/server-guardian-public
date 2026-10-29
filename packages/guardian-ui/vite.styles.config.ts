import { defineConfig } from 'vite';

// Stylesheet build (non-library mode so font files are emitted as real assets).
// Output: dist/guardian-ui.css plus dist/assets/*.woff2 (and .woff fallbacks).
export default defineConfig({
  base: './',
  build: {
    outDir: 'dist',
    emptyOutDir: false,
    assetsInlineLimit: 0,
    cssCodeSplit: false,
    rollupOptions: {
      input: 'src/styles/entry.ts',
      output: {
        entryFileNames: 'assets/.styles-entry.js',
        assetFileNames: (info) =>
          info.names?.some((n) => n.endsWith('.css'))
            ? 'guardian-ui.css'
            : 'assets/[name]-[hash][extname]',
      },
    },
  },
});
