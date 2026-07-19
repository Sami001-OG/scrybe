#!/usr/bin/env node
'use strict';
// Smoke test for the Node launcher layer (`npm test`).
//
// This checks the parts of the npm package that DON'T need a full Python
// provision: every module loads, the UI helpers behave, Python detection finds
// something on this machine, and the standalone-asset table covers the current
// platform. The heavy end-to-end flow (venv install + real conversion) is
// exercised separately; keeping this test light means `npm test` is fast and
// has no network/Python prerequisites beyond a system Python for the probe.

const assert = require('assert');
const path = require('path');

let failures = 0;
function check(name, fn) {
  try {
    fn();
    console.log('  ok   ' + name);
  } catch (e) {
    failures++;
    console.log('  FAIL ' + name + ' — ' + (e && e.message ? e.message : e));
  }
}

// 1. All modules load without throwing.
const ui = require('../lib/ui');
const python = require('../lib/python');
const env = require('../lib/env');

check('ui module exposes helpers', () => {
  ['banner', 'menu', 'ask', 'confirm', 'ok', 'warn', 'err', 'step'].forEach((k) => {
    assert.strictEqual(typeof ui[k], 'function', 'missing ui.' + k);
  });
  assert.ok(ui.banner().length > 0, 'banner is empty');
});

check('python module exposes API', () => {
  ['ensurePython', 'handscrybeHome', 'probe', 'systemCandidates'].forEach((k) => {
    assert.strictEqual(typeof python[k], 'function', 'missing python.' + k);
  });
});

check('systemCandidates is non-empty for this platform', () => {
  const cands = python.systemCandidates();
  assert.ok(Array.isArray(cands) && cands.length > 0, 'no candidates');
  cands.forEach((c) => assert.ok(Array.isArray(c) && c.length === 2, 'bad candidate shape'));
});

check('env module exposes API', () => {
  ['ensureVenv', 'venvPython', 'venvReady', 'payloadDir', 'payloadVersion'].forEach((k) => {
    assert.strictEqual(typeof env[k], 'function', 'missing env.' + k);
  });
});

check('payloadDir points at a dir containing pyproject.toml', () => {
  const fs = require('fs');
  const p = path.join(env.payloadDir(), 'pyproject.toml');
  assert.ok(fs.existsSync(p), 'pyproject.toml not found at payload root: ' + p);
});

check('payloadVersion matches package.json', () => {
  const pkg = require('../../package.json');
  assert.strictEqual(env.payloadVersion(), pkg.version);
});

check('bundled font ships inside the Python package', () => {
  const fs = require('fs');
  const ttf = path.join(env.payloadDir(), 'src', 'handscrybe', 'fonts', 'Caveat-Regular.ttf');
  assert.ok(fs.existsSync(ttf), 'bundled font missing: ' + ttf);
});

check('bin/handscrybe.js parses and references the launcher module', () => {
  const fs = require('fs');
  const src = fs.readFileSync(path.join(__dirname, '..', 'bin', 'handscrybe.js'), 'utf8');
  // It must hand off to the Python launcher (single stdin owner).
  assert.ok(src.includes('handscrybe.launcher'), 'launcher not referenced');
});

// A system Python probe is best-effort: on CI without Python this is allowed to
// find nothing, so we only assert it doesn't throw.
check('probe() runs without throwing', () => {
  const cands = python.systemCandidates();
  python.probe(cands[0][0], cands[0][1]); // may return null; must not throw
});

if (failures) {
  console.log('\n' + failures + ' check(s) failed.');
  process.exit(1);
}
console.log('\nAll smoke checks passed.');
