// Actual browser protocol + real WebCrypto, with a fake fetch and synthetic data.
const fs = require('node:fs');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');
const path = require('node:path');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const requests = [];
let reads = 0;
Object.defineProperty(globalThis, 'crypto', {configurable: true, value: {subtle: webcrypto.subtle, getRandomValues(array) {
  array.set(Array.from({length: array.length}, (_, index) => index)); return array;
}}});
globalThis.fetch = async (url, options) => {
  if (url.includes('/challenge?')) {
    requests.push({url, redirect: options.redirect});
    return {ok: true, json: async () => input.challenge};
  }
  requests.push({url, headers: options.headers, body: Buffer.from(options.body).toString('base64'), redirect: options.redirect});
  return {ok: input.postOK !== false, json: async () => ({node_id: 'synthetic-node'})};
};
vm.runInThisContext(fs.readFileSync(path.join(__dirname, '../browser-extension/capture-protocol.js'), 'utf8'));
(async () => {
  let error = null;
  try { await WorkflowyCapture.capture(input.token, async () => { reads++; return input.payload; }); }
  catch (failure) { error = failure.message; }
  process.stdout.write(JSON.stringify({requests, reads, error}));
})().catch(() => { process.exitCode = 1; });
