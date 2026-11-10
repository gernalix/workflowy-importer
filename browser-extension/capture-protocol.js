/* Shared browser-side protocol. The pairing key never leaves trusted contexts. */
globalThis.WorkflowyCapture = (() => {
  const endpoint = 'http://127.0.0.1:8765';
  const encoder = new TextEncoder();
  const hex = bytes => Array.from(new Uint8Array(bytes), b => b.toString(16).padStart(2, '0')).join('');
  const bytes = value => Uint8Array.from(value.match(/../g), part => parseInt(part, 16));
  const text = (domain, ...fields) => encoder.encode([domain, endpoint, ...fields].join('\0'));
  const validToken = value => typeof value === 'string' && /^[A-Za-z0-9_-]{43,128}$/.test(value);
  const validHex = value => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value);

  async function capture(token, payloadFactory) {
    if (!validToken(token)) throw new Error('Capture pairing required');
    const key = await crypto.subtle.importKey('raw', encoder.encode(token), {name: 'HMAC', hash: 'SHA-256'}, false, ['sign', 'verify']);
    const client = hex(crypto.getRandomValues(new Uint8Array(32)));
    const response = await fetch(`${endpoint}/challenge?client_nonce=${client}`, {
      redirect: 'error', credentials: 'omit', cache: 'no-store'
    });
    if (!response.ok) throw new Error('Capture authentication failed');
    const challenge = await response.json();
    const server = challenge.server_nonce;
    if (challenge.endpoint !== endpoint || !validHex(server) || !validHex(challenge.proof) ||
        !await crypto.subtle.verify('HMAC', key, bytes(challenge.proof), text('workflowy-server-v1', client, server))) {
      throw new Error('Capture authentication failed');
    }
    // Read the conversation only after authenticating the bridge.
    const payload = await payloadFactory();
    const plaintext = encoder.encode(JSON.stringify(payload));
    if (plaintext.byteLength > 5000000) throw new Error('Capture exceeds size limit');
    const rawEncryptionKey = await crypto.subtle.sign('HMAC', key, text('workflowy-encryption-v1', client, server));
    const encryptionKey = await crypto.subtle.importKey('raw', rawEncryptionKey, 'AES-GCM', false, ['encrypt']);
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const ciphertext = new Uint8Array(await crypto.subtle.encrypt({name: 'AES-GCM', iv,
      additionalData: text('workflowy-body-v1', client, server), tagLength: 128}, encryptionKey, plaintext));
    const digest = hex(await crypto.subtle.digest('SHA-256', ciphertext));
    const proof = hex(await crypto.subtle.sign('HMAC', key, text('workflowy-capture-v1', client, server, hex(iv), digest)));
    const result = await fetch(`${endpoint}/capture`, {
      method: 'POST', redirect: 'error', credentials: 'omit', cache: 'no-store',
      headers: {'Content-Type': 'application/octet-stream', 'X-Workflowy-Client': client,
        'X-Workflowy-Challenge': server, 'X-Workflowy-IV': hex(iv), 'X-Workflowy-Hash': digest,
        'X-Workflowy-Proof': proof}, body: ciphertext
    });
    // A failed POST is ambiguous. Never retry a remote mutation automatically.
    if (!result.ok) throw new Error('Capture failed; check Workflowy before trying again');
    return result.json();
  }
  return Object.freeze({capture, validToken});
})();
