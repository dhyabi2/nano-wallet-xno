// run: node tests/consent_check.js   (exit 0 = all checks pass)
const assert = require('assert');
const { xnoToRaw, buildConsent } = require('../docs/consent.js');
const ADDR = 'nano_39b311tznm9hub316enn8159ammduxnfonoaei7afzymw66wsouw9q3j9ic1';
const NOW = new Date('2026-11-02T10:00:00Z');
assert.strictEqual(xnoToRaw('0.05'), '50000000000000000000000000000');
assert.strictEqual(xnoToRaw('1'), '1000000000000000000000000000000');
assert.strictEqual(xnoToRaw('0.000000000000000000000000000001'), '1');
for (const bad of ['0', '0.0', '-1', '1e3', '0.05 ', '', '.5', '1.2.3', '0.0000000000000000000000000000001']) {
  if (bad === '0.05 ') { assert.strictEqual(xnoToRaw(bad), '50000000000000000000000000000'); continue; }
  assert.throws(() => xnoToRaw(bad), bad);
}
// receive-only never mentions a ceiling and says plainly it approves no spending
let r = buildConsent({ mode: 'receive-only', purpose: 'get paid', address: ADDR, approver: 'ops', now: NOW });
assert.ok(r.ok); assert.ok(/cannot spend/.test(r.consentText)); assert.ok(/not approved any spending/.test(r.consentText));
assert.ok(!/Ceiling/.test(r.consentText)); assert.strictEqual(r.commands.length, 1);
// spend window: exact raw ceiling in the command, expiry required and in the future
r = buildConsent({ mode: 'spend-window', purpose: 'p', address: ADDR, ceilingXno: '0.05', expires: '2026-12-01T00:00:00Z', approver: 'ops', approvalUrl: 'https://example.org/a', now: NOW });
assert.ok(r.ok); assert.strictEqual(r.ceilingRaw, '50000000000000000000000000000');
assert.ok(r.commands[1].includes('--ceiling-raw 50000000000000000000000000000') && r.commands[1].includes('--now 2026-11-02T10:00:00Z'));
assert.ok(/balance is the\nallowance/.test(r.consentText));
for (const [over, msg] of [[{ expires: '2026-11-01T00:00:00Z' }, /future/], [{ expires: 'tomorrow' }, /UTC/], [{ ceilingXno: '0.05e3' }, /ceiling/],
  [{ address: 'nano_x' }, /address/], [{ approver: ' ' }, /approving/], [{ purpose: '' }, /for/]]) {
  const x = buildConsent(Object.assign({ mode: 'spend-window', purpose: 'p', address: ADDR, ceilingXno: '0.05', expires: '2026-12-01T00:00:00Z', approver: 'ops', now: NOW }, over));
  assert.ok(!x.ok && x.errors.some(e => msg.test(e)), JSON.stringify(over));
}
// the page script does not touch the network or storage
const src = require('fs').readFileSync(__dirname + '/../docs/consent.js', 'utf8') + require('fs').readFileSync(__dirname + '/../docs/operator-consent.html', 'utf8');
for (const w of ['fetch(', 'XMLHttpRequest', 'localStorage', 'sessionStorage', 'sendBeacon', 'WebSocket']) assert.ok(!src.includes(w), w);
console.log('consent_check: all passed');
