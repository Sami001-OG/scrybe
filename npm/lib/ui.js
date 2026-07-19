'use strict';
// Tiny terminal UI helpers: colors, banners, and prompts.
//
// Deliberately dependency-free (no chalk/inquirer) so `npm install -g scrybe`
// pulls nothing and can never fail on dependency resolution. Colors degrade to
// plain text when stdout isn't a TTY or NO_COLOR is set, so piped/CI output
// stays clean.

const readline = require('readline');

const _color = process.stdout.isTTY && !process.env.NO_COLOR;
const wrap = (code) => (s) => (_color ? `\x1b[${code}m${s}\x1b[0m` : String(s));

const c = {
  bold: wrap('1'),
  dim: wrap('2'),
  red: wrap('31'),
  green: wrap('32'),
  yellow: wrap('33'),
  blue: wrap('34'),
  cyan: wrap('36'),
  gray: wrap('90'),
};

// The 'S' monogram + wordmark. Kept ASCII-only so it renders in every terminal
// (including Windows cmd.exe with legacy code pages).
function banner() {
  const art = [
    '   ____                _          ',
    '  / ___|  ___ _ __ _   _| |__   ___ ',
    "  \\___ \\ / __| '__| | | | '_ \\ / _ \\",
    '   ___) | (__| |  | |_| | |_) |  __/',
    '  |____/ \\___|_|   \\__, |_.__/ \\___|',
    '                   |___/            ',
  ];
  const out = art.map((l) => c.cyan(l)).join('\n');
  return out;
}

function heading(title) {
  return '\n' + c.bold(c.cyan('  ' + title)) + '\n';
}

function rule() {
  const w = Math.min(process.stdout.columns || 60, 60);
  return c.gray('  ' + '-'.repeat(Math.max(10, w - 4)));
}

function info(msg) {
  console.log('  ' + msg);
}
function ok(msg) {
  console.log('  ' + c.green('OK ') + msg);
}
function warn(msg) {
  console.log('  ' + c.yellow('!  ') + msg);
}
function err(msg) {
  console.log('  ' + c.red('X  ') + msg);
}
function step(msg) {
  console.log('  ' + c.blue('-> ') + msg);
}

// Ask a free-text question. Returns a trimmed string (may be empty).
function ask(question, def) {
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  const suffix = def ? c.gray(` [${def}]`) : '';
  return new Promise((resolve) => {
    rl.question('  ' + question + suffix + ' ', (answer) => {
      rl.close();
      const a = (answer || '').trim();
      resolve(a === '' && def !== undefined ? def : a);
    });
  });
}

// Present a numbered menu and return the chosen option's `value`.
// `options` is [{ label, value, hint?, disabled?, disabledNote? }].
async function menu(title, options) {
  console.log(heading(title));
  options.forEach((opt, i) => {
    const n = c.bold(c.cyan(String(i + 1) + ')'));
    if (opt.disabled) {
      console.log(
        '  ' + n + ' ' + c.gray(opt.label) + (opt.disabledNote ? '  ' + c.gray('(' + opt.disabledNote + ')') : '')
      );
    } else {
      console.log('  ' + n + ' ' + opt.label + (opt.hint ? '  ' + c.gray(opt.hint) : ''));
    }
  });
  console.log('');
  for (;;) {
    const raw = await ask(c.bold('Choose an option:'));
    const idx = parseInt(raw, 10);
    if (!Number.isNaN(idx) && idx >= 1 && idx <= options.length) {
      const chosen = options[idx - 1];
      if (chosen.disabled) {
        warn('That option is not available right now. Pick another.');
        continue;
      }
      return chosen.value;
    }
    warn('Please enter a number between 1 and ' + options.length + '.');
  }
}

// Yes/no confirmation. Returns boolean.
async function confirm(question, defYes) {
  const def = defYes ? 'Y/n' : 'y/N';
  const a = (await ask(question + ' ' + c.gray('(' + def + ')'), '')).toLowerCase();
  if (a === '') return !!defYes;
  return a === 'y' || a === 'yes';
}

module.exports = { c, banner, heading, rule, info, ok, warn, err, step, ask, menu, confirm };
