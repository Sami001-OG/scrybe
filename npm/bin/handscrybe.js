#!/usr/bin/env node
'use strict';
// Handscrybe launcher — the `handscrybe` command installed by `npm install -g handscrybe`.
//
// This Node process is a thin bootstrapper. Its only jobs:
//   1. Greet the user.
//   2. Guarantee a Python 3.12+ interpreter exists (find on the system, or
//      download a standalone build). See lib/python.js.
//   3. Guarantee a venv with Handscrybe + its deps is provisioned (first run only).
//      See lib/env.js.
//   4. Hand off *completely* to the Python launcher, inheriting the terminal.
//
// WHY THE MENU LIVES IN PYTHON, NOT HERE
// --------------------------------------
// The interactive menu and every prompt run inside a single Python process. If
// Node read stdin to show a menu and then spawned Python for the wizard, Node's
// line-buffered reader would swallow the answers Python expects and Python would
// see EOF. One process owns stdin — so Node never reads it.
//
// No npm dependencies, so `npm install -g handscrybe` is instant and can't fail on
// dependency resolution.

const { spawn } = require('child_process');
const ui = require('../lib/ui');
const { ensurePython } = require('../lib/python');
const { ensureVenv } = require('../lib/env');

async function main() {
  console.log('');
  console.log(ui.banner());
  console.log('');
  ui.info(ui.c.dim('Turn typed documents into handwriting — layout and all.'));

  // Provision Python + venv. These print their own progress and only do real
  // work on the very first run; afterwards they return in milliseconds.
  let vpy;
  try {
    const py = await ensurePython();
    vpy = await ensureVenv(py);
  } catch (e) {
    console.log('');
    ui.err('Setup could not finish:');
    console.log('  ' + String(e.message || e).split('\n').join('\n  '));
    console.log('');
    ui.info('If this persists, install Python 3.12+ yourself and re-run ' + ui.c.bold('handscrybe') + '.');
    process.exit(1);
  }

  // Hand off to the Python menu. stdio:'inherit' gives Python the real terminal
  // so all prompts, colors, and the Flask log stream through directly, and this
  // Node process reads nothing from stdin.
  const child = spawn(vpy, ['-m', 'handscrybe.launcher'], { stdio: 'inherit' });
  child.on('exit', (code) => process.exit(code || 0));
  child.on('error', (err) => {
    ui.err('Could not start Handscrybe: ' + err.message);
    process.exit(1);
  });
}

main().catch((e) => {
  ui.err('Unexpected error: ' + (e && e.message ? e.message : e));
  process.exit(1);
});
