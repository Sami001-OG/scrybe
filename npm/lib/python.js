'use strict';
// Locate a usable Python 3.12+, or download a standalone CPython build.
//
// WHY THIS EXISTS
// ---------------
// Handscrybe's engine is Python (PyMuPDF, numpy, Flask — no JS equivalents). The
// npm package is a launcher; the real work runs in Python. So the very first
// thing `handscrybe` must do is guarantee a Python 3.12+ interpreter exists.
//
// STRATEGY (cheapest first)
//   1. Reuse a system Python 3.12+ if one is already installed. No download,
//      instant. This is the common case for developers.
//   2. Otherwise download a *relocatable* standalone CPython (the same
//      python-build-standalone builds uv/pyenv use) into ~/.handscrybe/. No admin
//      rights, no PATH changes, fully self-contained.
//
// Everything is cached under ~/.handscrybe so provisioning happens once.

const { execFileSync, spawnSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const https = require('https');
const ui = require('./ui');

const MIN_MINOR = 12; // require CPython 3.12+

// --- Handscrybe home ---------------------------------------------------------
function handscrybeHome() {
  const home = process.env.HANDSCRYBE_HOME || path.join(os.homedir(), '.handscrybe');
  fs.mkdirSync(home, { recursive: true });
  return home;
}

// Candidate interpreter commands to probe on the system PATH, best first.
function systemCandidates() {
  if (process.platform === 'win32') {
    // The `py` launcher can select an exact version; also try bare names.
    return [
      ['py', ['-3.12']],
      ['py', ['-3']],
      ['python3.12', []],
      ['python', []],
      ['python3', []],
    ];
  }
  return [
    ['python3.12', []],
    ['python3.13', []],
    ['python3', []],
    ['python', []],
  ];
}

// Return {cmd, args, version:[maj,min]} if this candidate is Python >= 3.12.
function probe(cmd, baseArgs) {
  try {
    const args = baseArgs.concat([
      '-c',
      'import sys;print("%d.%d"%(sys.version_info[0],sys.version_info[1]))',
    ]);
    const out = execFileSync(cmd, args, {
      stdio: ['ignore', 'pipe', 'ignore'],
      timeout: 8000,
    })
      .toString()
      .trim();
    const m = out.match(/^(\d+)\.(\d+)$/);
    if (!m) return null;
    const maj = parseInt(m[1], 10);
    const min = parseInt(m[2], 10);
    if (maj === 3 && min >= MIN_MINOR) return { cmd, args: baseArgs, version: [maj, min] };
    return null;
  } catch (_) {
    return null;
  }
}

// Path to a previously-provisioned standalone interpreter, if present.
function managedPython() {
  const home = handscrybeHome();
  const base = path.join(home, 'python');
  const exe =
    process.platform === 'win32'
      ? path.join(base, 'python', 'python.exe')
      : path.join(base, 'python', 'bin', 'python3');
  return fs.existsSync(exe) ? exe : null;
}

// --- Standalone CPython download ----------------------------------------
// python-build-standalone release assets. Pinned to a known-good release so the
// URL is stable and reproducible; updated deliberately, never floating.
const PBS_TAG = '20241016';
const PBS_PY = '3.12.7';

function pbsAsset() {
  const plat = process.platform;
  const arch = process.arch; // 'x64' | 'arm64'
  const triples = {
    'win32:x64': 'x86_64-pc-windows-msvc-shared-install_only',
    'darwin:x64': 'x86_64-apple-darwin-install_only',
    'darwin:arm64': 'aarch64-apple-darwin-install_only',
    'linux:x64': 'x86_64-unknown-linux-gnu-install_only',
    'linux:arm64': 'aarch64-unknown-linux-gnu-install_only',
  };
  const key = `${plat}:${arch}`;
  const triple = triples[key];
  if (!triple) return null;
  const name = `cpython-${PBS_PY}+${PBS_TAG}-${triple}.tar.gz`;
  const url = `https://github.com/indygreg/python-build-standalone/releases/download/${PBS_TAG}/${name}`;
  return { url, name };
}

function download(url, dest) {
  return new Promise((resolve, reject) => {
    const file = fs.createWriteStream(dest);
    const get = (u, redirects) => {
      if (redirects > 10) return reject(new Error('Too many redirects'));
      https
        .get(u, { headers: { 'User-Agent': 'handscrybe-installer' } }, (res) => {
          if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
            res.resume();
            return get(res.headers.location, redirects + 1);
          }
          if (res.statusCode !== 200) {
            res.resume();
            return reject(new Error(`Download failed: HTTP ${res.statusCode}`));
          }
          const total = parseInt(res.headers['content-length'] || '0', 10);
          let seen = 0;
          let lastPct = -1;
          res.on('data', (chunk) => {
            seen += chunk.length;
            if (total && process.stdout.isTTY) {
              const pct = Math.floor((seen / total) * 100);
              if (pct !== lastPct && pct % 5 === 0) {
                lastPct = pct;
                process.stdout.write(`\r  ${ui.c.blue('->')} downloading Python… ${pct}%`);
              }
            }
          });
          res.pipe(file);
          file.on('finish', () => {
            if (process.stdout.isTTY) process.stdout.write('\r' + ' '.repeat(40) + '\r');
            file.close(() => resolve());
          });
        })
        .on('error', (e) => {
          fs.unlink(dest, () => reject(e));
        });
    };
    get(url, 0);
  });
}

// Extract a .tar.gz using the system `tar` (present on Windows 10+, macOS, Linux).
function extractTarGz(archive, destDir) {
  fs.mkdirSync(destDir, { recursive: true });
  const r = spawnSync('tar', ['-xzf', archive, '-C', destDir], { stdio: 'ignore' });
  if (r.status !== 0) throw new Error('Extraction failed (is `tar` available?)');
}

async function downloadStandalone() {
  const asset = pbsAsset();
  if (!asset) {
    throw new Error(
      `No prebuilt Python is available for ${process.platform}/${process.arch}. ` +
        'Please install Python 3.12+ manually and re-run handscrybe.'
    );
  }
  const home = handscrybeHome();
  const base = path.join(home, 'python');
  fs.mkdirSync(base, { recursive: true });
  const archive = path.join(base, asset.name);

  ui.step('No Python 3.12+ found — fetching a private copy (one time, ~30 MB).');
  await download(asset.url, archive);
  ui.step('Unpacking Python…');
  extractTarGz(archive, base);
  try {
    fs.unlinkSync(archive);
  } catch (_) {}

  const exe =
    process.platform === 'win32'
      ? path.join(base, 'python', 'python.exe')
      : path.join(base, 'python', 'bin', 'python3');
  if (!fs.existsSync(exe)) {
    throw new Error('Python was downloaded but the interpreter was not found after extraction.');
  }
  return exe;
}

// --- Public API ----------------------------------------------------------
// Resolve a Python interpreter, provisioning one if necessary.
// Returns { cmd, args } ready to spawn (args is [] for a direct executable).
async function ensurePython() {
  // 1. Already-provisioned managed interpreter (fast path after first run).
  const managed = managedPython();
  if (managed) return { cmd: managed, args: [] };

  // 2. A suitable system Python.
  for (const [cmd, baseArgs] of systemCandidates()) {
    const hit = probe(cmd, baseArgs);
    if (hit) return { cmd: hit.cmd, args: hit.args };
  }

  // 3. Download a standalone build.
  const exe = await downloadStandalone();
  return { cmd: exe, args: [] };
}

module.exports = { ensurePython, handscrybeHome, probe, systemCandidates };
