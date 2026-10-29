// Builds the in-repo design system (packages/guardian-ui) so the app can import its dist output.
//   node scripts/build-ui.mjs              always rebuild (used by `prebuild`)
//   node scripts/build-ui.mjs --if-missing only build when dist/ does not exist (used by `pretest`)
// `npm ci` for the design system is skipped when its node_modules already exists.
import { existsSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const uiDir = resolve(here, '../../../packages/guardian-ui');
const npm = process.platform === 'win32' ? 'npm.cmd' : 'npm';

if (process.argv.includes('--if-missing') && existsSync(resolve(uiDir, 'dist/index.js'))) {
  process.exit(0);
}

function run(args) {
  const res = spawnSync(npm, ['--prefix', uiDir, ...args], { stdio: 'inherit' });
  if (res.status !== 0) process.exit(res.status ?? 1);
}

if (!existsSync(resolve(uiDir, 'node_modules'))) run(['ci']);
run(['run', 'build']);
