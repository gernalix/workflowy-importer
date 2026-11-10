# ChatGPT → Workflowy browser helper

This optional unpacked Chrome/Chromium extension exports only the currently open ChatGPT conversation. It never stores or receives the Workflowy API key.

1. Before a separately authorized deployment, configure a dedicated random
   `WORKFLOWY_CAPTURE_TOKEN` in the existing `FEDORA_WORKFLOWY_ENV` Bitwarden
   runtime reference. Never reuse the Workflowy API key. See the root README.
2. Run the updated `wf serve` locally with its runtime dependency `cryptography`.
   The extension uses the fixed endpoint `127.0.0.1:8765`.
3. Load this directory as an unpacked extension in Chrome/Chromium 106+.
4. Open extension options and enter that dedicated capture key from Bitwarden.
   Pairing is held only in trusted extension session storage; browser restart or
   extension reload requires pairing again. The input clears immediately.
5. Open a ChatGPT conversation and click the extension action.

The extension authenticates the bridge before extracting visible user/assistant
turns, sends an authenticated encrypted capture, and the bridge performs the
Workflowy write. Neither key is sent to the content script or page. The pairing
key never travels over HTTP; a substituted/relaying local listener receives only
ciphertext. No automatic retry follows an uncertain remote mutation.
If ChatGPT changes its DOM attributes, update `content.js`; the account-wide JSON
importer (`wf chatgpt`) remains the stable fallback.

This source patch does not create a vault field, pair a real browser, install the
extension, restart a current service or prove a live capture. See
[CAPTURE_PROTOCOL.md](CAPTURE_PROTOCOL.md) for compatibility details.
