// Drop legacy .woff fallbacks from the built stylesheet: every supported
// webview (WKWebView, WebView2, Chromium) loads woff2, and the extra files
// double the size of the font payload shipped with the package.
import { readFileSync, writeFileSync, readdirSync, rmSync } from 'node:fs';
import { join } from 'node:path';

const dist = new URL('../dist/', import.meta.url).pathname;
const cssPath = join(dist, 'guardian-ui.css');

const css = readFileSync(cssPath, 'utf8');
const stripped = css.replace(/,\s*url\([^)]*\.woff\)\s*format\(["']woff["']\)/g, '');
writeFileSync(cssPath, stripped);

const assets = join(dist, 'assets');
let removed = 0;
for (const name of readdirSync(assets)) {
  if (name.endsWith('.woff')) {
    rmSync(join(assets, name));
    removed++;
  }
}

if (/\.woff\)/.test(stripped)) {
  console.error('strip-woff: stylesheet still references .woff files');
  process.exit(1);
}
console.log(`strip-woff: removed ${removed} .woff files`);
