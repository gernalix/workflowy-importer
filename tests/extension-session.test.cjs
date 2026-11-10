const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const source = name => fs.readFileSync(path.join(__dirname, '../browser-extension', name), 'utf8');
const token = 'synthetic_capture_pairing_key_' + 'x'.repeat(32);

function background(paired, failure = false) {
  const calls = {exports: [], captures: [], options: 0, badges: [], logs: [], access: []};
  const chrome = {
    action: {onClicked: {addListener(listener) {calls.click = listener;}},
      async setBadgeText(value) {calls.badges.push(value.text);}},
    runtime: {async openOptionsPage() {calls.options++;}},
    tabs: {async sendMessage(id, message) {calls.exports.push(message); return {ok: true, payload: {title: 'synthetic'}};}},
    storage: {session: {
      async setAccessLevel(value) {calls.access.push(value.accessLevel);},
      async get() {return paired ? {captureToken: token} : {};}
    }}
  };
  const context = vm.createContext({chrome, importScripts() {}, setTimeout() {},
    WorkflowyCapture: {validToken: value => value === token,
      async capture(key, payloadFactory) {
        assert.equal(key, token); calls.captures.push(await payloadFactory());
        if (failure) throw new Error(token + ' PRIVATE_BODY');
      }}, console: {error(...args) {calls.logs.push(args);}}});
  vm.runInContext(source('background.js'), context);
  return calls;
}

test('unpaired click opens trusted options before any conversation export', async () => {
  const calls = background(false);
  await calls.click({id: 1, url: 'https://chatgpt.com/c/synthetic'});
  assert.equal(calls.options, 1);
  assert.equal(calls.exports.length, 0);
  assert.equal(calls.captures.length, 0);
  assert.deepEqual(calls.access, ['TRUSTED_CONTEXTS']);
});

test('paired key remains in trusted worker and never enters content messages', async () => {
  const calls = background(true);
  await calls.click({id: 1, url: 'https://chatgpt.com/c/synthetic'});
  assert.equal(calls.exports.length, 1);
  assert.equal(calls.captures.length, 1);
  assert.equal(JSON.stringify(calls.exports).includes(token), false);
  assert.deepEqual(calls.badges, ['✓']);
});

test('worker logs only generic failure and never retries capture', async () => {
  const calls = background(true, true);
  await calls.click({id: 1, url: 'https://chatgpt.com/c/synthetic'});
  assert.equal(calls.captures.length, 1);
  assert.equal(JSON.stringify(calls.logs).includes(token), false);
  assert.equal(JSON.stringify(calls.logs).includes('PRIVATE_BODY'), false);
  assert.deepEqual(calls.badges, ['!']);
});

test('other pages cannot trigger capture', async () => {
  const calls = background(true);
  await calls.click({id: 1, url: 'https://chatgpt.com.example.invalid/'});
  assert.equal(calls.exports.length, 0);
  assert.equal(calls.captures.length, 0);
});

function options(failure = false) {
  const elements = Object.fromEntries(['token', 'status', 'pairing', 'forget'].map(name => [name, {
    value: '', textContent: '', listeners: {}, addEventListener(kind, callback) {this.listeners[kind] = callback;}
  }]));
  const stored = {};
  const access = [];
  const context = vm.createContext({document: {getElementById: name => elements[name]},
    WorkflowyCapture: {validToken: value => value === token},
    chrome: {storage: {session: {
      async setAccessLevel(value) {access.push(value.accessLevel);},
      async set(value) {if (failure) throw new Error(token); Object.assign(stored, value);},
      async remove(name) {delete stored[name];}
    }}}});
  vm.runInContext(source('options.js'), context);
  return {elements, stored, access};
}

test('pairing uses session storage, clears the password field and can be forgotten', async () => {
  const fixture = options();
  fixture.elements.token.value = token;
  await fixture.elements.pairing.listeners.submit({preventDefault() {}});
  assert.equal(fixture.elements.token.value, '');
  assert.equal(fixture.stored.captureToken, token);
  assert.deepEqual(fixture.access, ['TRUSTED_CONTEXTS']);
  assert.equal(fixture.elements.status.textContent.includes(token), false);
  await fixture.elements.forget.listeners.click();
  assert.equal('captureToken' in fixture.stored, false);
});

test('pairing failure clears input without rendering the key', async () => {
  const fixture = options(true);
  fixture.elements.token.value = token;
  await fixture.elements.pairing.listeners.submit({preventDefault() {}});
  assert.equal(fixture.elements.token.value, '');
  assert.equal(fixture.elements.status.textContent.includes(token), false);
  assert.equal('captureToken' in fixture.stored, false);
});
