# Local capture protocol v1

The server loads a dedicated random 256-bit-or-stronger base64url key from the
Fedora Bitwarden field `WORKFLOWY_CAPTURE_TOKEN`. The trusted extension holds the
same key in `chrome.storage.session`, never persistent/local/sync storage. No key
is transported over HTTP. Different local UIDs are untrusted. Same-UID account,
browser/extension compromise or an authorized client's deliberate key disclosure
are outside this boundary. An untrusted listener can still block or drop traffic.

All transcript fields are ASCII and separated by one NUL byte. Nonces and hashes
use lowercase hexadecimal. The endpoint is derived from the server's actual
bound socket, never a request Host value; the extension expects exactly
`http://127.0.0.1:8765`. Alternate clients must expect their actual endpoint.

1. Client generates 32 random bytes `C` and GETs `/challenge?client_nonce=C`.
   Server generates 32 random bytes `S` and replies with `server_nonce`, `endpoint`
   and hex HMAC-SHA256(key, `workflowy-server-v1`, endpoint, C, S).
   Client verifies the MAC before collecting any private conversation.
2. Derive a 32-byte AES key as HMAC-SHA256(key,
   `workflowy-encryption-v1`, endpoint, C, S). Encode JSON once as UTF-8.
   AES-256-GCM encrypts it using a fresh random 12-byte IV, 128-bit tag and AAD
   (`workflowy-body-v1`, endpoint, C, S). The HTTP body is ciphertext with tag
   appended. It contains no plaintext, including during a same-endpoint relay.
3. POST `/capture` with `Content-Type: application/octet-stream` and headers:
   `X-Workflowy-Client: C`, `X-Workflowy-Challenge: S`, `X-Workflowy-IV: IV`,
   `X-Workflowy-Hash: SHA256(ciphertext)` and `X-Workflowy-Proof` containing hex
   HMAC-SHA256(key, `workflowy-capture-v1`, endpoint, C, S, IV, hash).
4. Server rejects duplicate/malformed headers and expired/unknown challenges,
   verifies the proof before reading any body, then atomically consumes the
   challenge. It checks bounded body length/hash and the GCM tag before parsing
   JSON or invoking routing/Workflowy. Challenges expire after 60 seconds;
   at most 256 are retained. Reads time out after 10 seconds. POST responses
   contain only created-node ID/rule or a generic failure, never reflected errors.

Origin/CORS filtering remains an additional browser defense. Preflight and
minimal `/health` are public and non-mutating. Challenge responses are non-cacheable.
Fetches reject redirects and do not attach browser credentials. A POST is never
automatically replayed: a failed response may follow a successful remote write.
Server restart invalidates challenges. Key rotation requires a separately
authorized restart and new browser pairing. This protocol changes the old
unauthenticated JSON interface; there is no insecure compatibility mode.

Implementations reuse [cryptography AESGCM](https://cryptography.io/en/stable/hazmat/primitives/aead/)
and browser WebCrypto. Tests exercise actual WebCrypto against Python encryption,
MAC, challenge expiry, tampering and replay checks, using only synthetic keys.
[Chrome session storage](https://developer.chrome.com/docs/extensions/reference/api/storage)
keeps pairing in memory and restricts it to trusted extension contexts.
