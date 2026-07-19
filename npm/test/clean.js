#!/usr/bin/env node
'use strict';
// Remove Python build artifacts before packing/publishing.
//
// npm's `files` whitelist in package.json pulls all of `src/`, so any
// __pycache__/ or *.egg-info/ directory that a local `pip install` regenerated
// would otherwise be published. This script scrubs them so the tarball only ever
// contains real source. Wired into `prepublishOnly` so it runs automatically on
// every `npm publish`, and safe to run by hand any time.

const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..', '..');
// Only scrub the dirs that actually ship in the npm tarball (src/) plus tests/.
// Never descend into virtualenvs or dependency trees — those caches are huge,
// irrelevant to the package, and regenerate on demand.
const SCOPE = ['src', 'tests'];
const TARGETS = new Set(['__pycache__', '.pytest_cache']);
const SUFFIXES = ['.egg-info'];
const SKIP = new Set(['node_modules', '.git', '.venv', 'venv', 'env']);

let removed = 0;

function scrub(dir) {
  let entries;
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch (_) {
    return;
  }
  for (const ent of entries) {
    if (!ent.isDirectory()) continue;
    if (SKIP.has(ent.name)) continue;
    const full = path.join(dir, ent.name);
    const isTarget = TARGETS.has(ent.name) || SUFFIXES.some((s) => ent.name.endsWith(s));
    if (isTarget) {
      fs.rmSync(full, { recursive: true, force: true });
      removed += 1;
      console.log('  removed ' + path.relative(ROOT, full));
    } else {
      scrub(full);
    }
  }
}

for (const s of SCOPE) scrub(path.join(ROOT, s));
console.log(removed ? `Cleaned ${removed} artifact dir(s).` : 'Nothing to clean.');
