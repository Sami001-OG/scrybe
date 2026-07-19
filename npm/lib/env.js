'use strict';
// Provision a Python virtual environment with Handscrybe and its dependencies.
//
// Given a working Python interpreter (from python.js), this creates a venv under
// ~/.handscrybe/venv, installs the bundled Handscrybe payload into it (which pulls the
// pinned deps: PyMuPDF, numpy, Flask, ...), and records a version marker so the
// whole step is skipped on later runs. The venv's own python is then used to run
// the CLI, wizard, and web server — fully isolated from any system packages.

const { spawnSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const ui = require('./ui');
const { handscrybeHome } = require('./python');

// The bundled Python payload is the npm package root itself: it ships
// pyproject.toml, src/, and fonts/ (see package.json "files"), which is exactly
// what `pip install <dir>` needs. __dirname is npm/lib, so two levels up is the
// package root.
function payloadDir() {
  return path.join(__dirname, '..', '..');
}

// The installed package version, used as the venv marker so a Handscrybe upgrade
// (new npm version) triggers a reinstall automatically.
function payloadVersion() {
  try {
    const pkg = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', 'package.json'), 'utf8'));
    return pkg.version || '0.0.0';
  } catch (_) {
    return '0.0.0';
  }
}

function venvDir() {
  return path.join(handscrybeHome(), 'venv');
}

// Path to the venv's python executable (platform-specific layout).
function venvPython() {
  const base = venvDir();
  return process.platform === 'win32'
    ? path.join(base, 'Scripts', 'python.exe')
    : path.join(base, 'bin', 'python');
}

function markerPath() {
  return path.join(venvDir(), '.handscrybe-installed');
}

// Is a venv already provisioned for the current payload version?
function venvReady() {
  try {
    if (!fs.existsSync(venvPython())) return false;
    const marker = fs.readFileSync(markerPath(), 'utf8').trim();
    return marker === payloadVersion();
  } catch (_) {
    return false;
  }
}

// Run a command, streaming nothing but capturing output; return {ok, out}.
function run(cmd, args) {
  const r = spawnSync(cmd, args, {
    stdio: ['ignore', 'pipe', 'pipe'],
    encoding: 'utf8',
    timeout: 600000, // 10 min ceiling for a first-time dependency install
  });
  const out = (r.stdout || '') + (r.stderr || '');
  return { ok: r.status === 0, out, status: r.status };
}

// Create the venv and install Handscrybe into it. `py` is {cmd, args} from python.js.
async function ensureVenv(py) {
  if (venvReady()) return venvPython();

  const vdir = venvDir();
  // A stale/partial venv from an interrupted install must be cleared first, or
  // `venv` will refuse to recreate it and pip may see half-installed packages.
  if (fs.existsSync(vdir)) {
    fs.rmSync(vdir, { recursive: true, force: true });
  }

  ui.step('Setting up Handscrybe (first run only). This can take a minute…');

  // 1. Create the venv using the resolved interpreter.
  const create = run(py.cmd, py.args.concat(['-m', 'venv', vdir]));
  if (!create.ok) {
    throw new Error('Could not create the Python environment:\n' + tail(create.out));
  }

  const vpy = venvPython();

  // 2. Upgrade pip so wheels for PyMuPDF/numpy resolve cleanly on all platforms.
  ui.step('Preparing package installer…');
  const up = run(vpy, ['-m', 'pip', 'install', '--upgrade', 'pip', '--quiet']);
  if (!up.ok) {
    // Non-fatal: an older pip usually still installs fine. Warn and continue.
    ui.warn('Could not upgrade pip; continuing with the bundled version.');
  }

  // 3. Install the bundled payload (pulls all pinned deps from pyproject.toml).
  ui.step('Installing Handscrybe and its dependencies…');
  const install = run(vpy, ['-m', 'pip', 'install', payloadDir(), '--quiet']);
  if (!install.ok) {
    throw new Error('Dependency installation failed:\n' + tail(install.out));
  }

  // 4. Sanity check: the package imports and fonts resolve.
  const check = run(vpy, [
    '-c',
    'import handscrybe.pipeline, handscrybe.webapp, handscrybe.wizard; print("ok")',
  ]);
  if (!check.ok || !check.out.includes('ok')) {
    throw new Error('Handscrybe was installed but failed to import:\n' + tail(check.out));
  }

  fs.mkdirSync(vdir, { recursive: true });
  fs.writeFileSync(markerPath(), payloadVersion());
  ui.ok('Handscrybe is ready.');
  return vpy;
}

// Keep only the last few lines of a captured output for concise error messages.
function tail(s, n) {
  const lines = String(s).trim().split(/\r?\n/);
  return lines.slice(-(n || 8)).join('\n');
}

module.exports = { ensureVenv, venvPython, venvReady, payloadDir, payloadVersion, venvDir };
