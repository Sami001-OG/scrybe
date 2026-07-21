'use strict';
// Tiny terminal UI helpers: colors, banners, and prompts.
//
// Deliberately dependency-free (no chalk/inquirer) so `npm install -g handscrybe`
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

// Boxed wordmark. Kept to a simple ASCII box (no figlet art) so the name is
// always spelled correctly and renders in every terminal, including Windows
// cmd.exe with legacy code pages.
// The "HS" wordmark drawn in heavy box-drawing block characters. This is the
// product's logo; when the terminal can render Unicode it leads the banner.
const _HS_LOGO = [
  '██╗  ██╗███████╗',
  '██║  ██║██╔════╝',
  '███████║███████╗',
  '██╔══██║╚════██║',
  '██║  ██║███████║',
  '╚═╝  ╚═╝╚══════╝',
];

// Whether we can safely print the block-character logo. The logo uses U+2588
// block and U+2550-range box-drawing glyphs that legacy Windows code pages
// (cmd.exe on CP437/CP850) render as mojibake, so we gate on evidence of a
// UTF-8-capable terminal:
//   * any non-Windows TTY (modern *nix/macOS terminals are UTF-8),
//   * Windows Terminal / VS Code / other modern hosts that advertise UTF-8
//     via WT_SESSION, TERM_PROGRAM, or a UTF-8 locale.
// When unsure we fall back to the plain ASCII wordmark, which renders anywhere.
// Non-TTY output (piped/redirected) always takes the ASCII path so logs stay
// clean. NO_COLOR only strips color, not the Unicode art — an uncolored logo
// still renders fine on a UTF-8 terminal.
function _unicodeOK() {
  if (!process.stdout.isTTY) return false;
  if (process.platform !== 'win32') return true;
  if (process.env.WT_SESSION || process.env.TERM_PROGRAM) return true;
  const enc = (process.env.LANG || process.env.LC_ALL || process.env.LC_CTYPE || '');
  return /utf-?8/i.test(enc);
}

// Center a line inside `width` columns (extra space biased right, like the box).
function _center(s, width) {
  const pad = Math.max(0, width - s.length);
  const left = Math.floor(pad / 2);
  return ' '.repeat(left) + s + ' '.repeat(pad - left);
}

// Boxed wordmark. When the terminal is UTF-8-capable we show the "HS" block
// logo above the name; otherwise we degrade to a pure-ASCII box so the name is
// always spelled correctly and renders in every terminal, including legacy
// Windows cmd.exe. Borders are built programmatically so they always line up.
function banner() {
  const name = 'H A N D S C R Y B E';
  const tag = 'typed documents -> your handwriting';
  const useLogo = _unicodeOK();

  // Inner width accommodates the widest of: the logo art, the name, the tagline.
  const logoWidth = useLogo ? Math.max(..._HS_LOGO.map((l) => l.length)) : 0;
  const width = Math.max(logoWidth, name.length, tag.length) + 4; // 2 spaces padding each side
  const bar = (useLogo ? '═' : '-').repeat(width);
  const [tl, tr, bl, br, v] = useLogo
    ? ['╔', '╗', '╚', '╝', '║']
    : ['+', '+', '+', '+', '|'];
  const box = (s) => v + _center(s, width) + v;

  const lines = [tl + bar + tr];
  if (useLogo) {
    for (const l of _HS_LOGO) lines.push(box(l));
    lines.push(box(''));
  }
  lines.push(box(name));
  lines.push(box(tag));
  lines.push(bl + bar + br);

  return lines.map((l) => '  ' + c.cyan(l)).join('\n');
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
