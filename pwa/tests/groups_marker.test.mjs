import test from 'node:test';
import assert from 'node:assert/strict';

globalThis.location = { search: '?mock=1&runs=manual' };
globalThis.window = {};
const { parseGroupMarker } = await import('../groups.js');

test('parses leading AGREE markers in English and Russian, case-insensitively', () => {
  for (const input of ['[AGREE] Keep it simple.', '[agree] Keep it simple.', '[СОГЛАСЕН] Оставим просто.', '[согласен] Оставим просто.']) {
    const expected = input.endsWith('.') ? input.slice(input.indexOf(']') + 1).trimStart() : '';
    assert.deepEqual(parseGroupMarker(input), { marker: 'agree', text: expected });
  }
});

test('parses leading PASS markers and removes adjacent whitespace', () => {
  assert.deepEqual(parseGroupMarker(' \n [PASS]\t\n Останавливаюсь.'), { marker: 'pass', text: 'Останавливаюсь.' });
  assert.deepEqual(parseGroupMarker('[ПАС]'), { marker: 'pass', text: '' });
  assert.deepEqual(parseGroupMarker('  [пАс] \t  '), { marker: 'pass', text: '' });
});

test('leaves absent, embedded, and non-matching markers unchanged', () => {
  for (const text of ['', 'plain reply', 'Text before [PASS]', '[AGREED] almost', '[PASS extra] nope']) {
    assert.deepEqual(parseGroupMarker(text), { marker: null, text });
  }
});

test('non-string input becomes empty text', () => {
  assert.deepEqual(parseGroupMarker(null), { marker: null, text: '' });
});
