// Pure functions behind docs/operator-consent.html. No DOM, no network, no storage: the page runs entirely in the browser.
// 1 XNO = 10**30 raw. Amounts are parsed from a decimal string with BigInt; a float is never used.
(function (root) {
  'use strict';
  var RAW = 10n ** 30n;
  var ADDR = /^nano_[13][13456789abcdefghijkmnopqrstuwxyz]{59}$/;

  function xnoToRaw(text) {
    var t = String(text).trim();
    if (!/^\d+(\.\d{1,30})?$/.test(t)) throw new Error('amount must be a plain decimal such as 0.05 (at most 30 decimals)');
    var parts = t.split('.');
    var frac = (parts[1] || '').padEnd(30, '0');
    var raw = BigInt(parts[0]) * RAW + BigInt(frac);
    if (raw <= 0n) throw new Error('amount must be above zero');
    return raw.toString();
  }

  function isoUtc(d) { return d.toISOString().replace(/\.\d{3}Z$/, 'Z'); }

  // form: {mode: 'receive-only'|'spend-window', purpose, address, ceilingXno, expires (ISO UTC), approver, approvalUrl, now (Date)}
  function buildConsent(form) {
    var errors = [];
    var mode = form.mode === 'spend-window' ? 'spend-window' : 'receive-only';
    var purpose = String(form.purpose || '').trim();
    var approver = String(form.approver || '').trim();
    var address = String(form.address || '').trim();
    if (!purpose) errors.push('say what the account is for');
    if (!approver) errors.push('say who is approving');
    if (!ADDR.test(address)) errors.push('the address must look like nano_ followed by 60 characters (the checksum is checked by the command below)');
    var now = form.now instanceof Date ? form.now : new Date();
    var out = { ok: false, errors: errors, mode: mode };
    var ceilingRaw = null, expires = null;
    if (mode === 'spend-window') {
      try { ceilingRaw = xnoToRaw(form.ceilingXno); } catch (e) { errors.push('ceiling: ' + e.message); }
      expires = String(form.expires || '').trim();
      if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(expires)) errors.push('expiry must be UTC like 2026-12-01T00:00:00Z');
      else if (expires <= isoUtc(now)) errors.push('expiry must be in the future');
    }
    if (errors.length) return out;
    var nowIso = isoUtc(now);
    var lines = [];
    lines.push('# Operator consent (' + mode + ')');
    lines.push('');
    lines.push('Approved by: ' + approver + ' on ' + nowIso);
    lines.push('Purpose: ' + purpose);
    lines.push('Address: ' + address);
    if (mode === 'receive-only') {
      lines.push('');
      lines.push('I approve this agent publishing the address above to RECEIVE XNO. The agent holds no key for it and cannot spend from it.');
      lines.push('I have not approved any spending.');
    } else {
      lines.push('Ceiling: ' + String(form.ceilingXno).trim() + ' XNO (' + ceilingRaw + ' raw), funded once, nothing above it');
      lines.push('Expires: ' + expires);
      lines.push('');
      lines.push('I approve the agent spending from this account until it is empty or the expiry passes, whichever is first. The balance is the');
      lines.push('allowance: there is nothing above the ceiling to spend. I can end this at any time by sweeping the remainder back to my own account.');
    }
    lines.push('');
    lines.push('Post this text somewhere anyone can read (an issue comment, a gist, a page on your site). That URL is your approval record.');
    out.ok = true;
    out.consentText = lines.join('\n');
    out.commands = ['python3 cli.py check ' + address];
    if (mode === 'spend-window') {
      var url = String(form.approvalUrl || '').trim() || 'https://WHERE-YOU-POSTED-THE-TEXT';
      out.commands.push('python3 spend_window.py open --purpose ' + JSON.stringify(purpose) + ' --ceiling-raw ' + ceilingRaw +
        ' --address ' + address + ' --approved-by ' + JSON.stringify(approver) + ' --approved-at ' + nowIso +
        ' --approval-url ' + url + ' --expires ' + expires + ' --now ' + nowIso);
    }
    out.ceilingRaw = ceilingRaw;
    return out;
  }

  var api = { xnoToRaw: xnoToRaw, buildConsent: buildConsent };
  if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.OperatorConsent = api;
})(typeof window !== 'undefined' ? window : globalThis);
