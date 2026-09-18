# ChatGPT → Workflowy browser helper

This optional unpacked Chrome/Chromium extension exports only the currently open ChatGPT conversation. It never stores or receives the Workflowy API key.

1. Run `wf serve` locally. The bridge binds to `127.0.0.1:8765` only.
2. Load this directory as an unpacked extension.
3. Open a ChatGPT conversation and click the extension action.

The extension extracts visible user/assistant turns, sends Markdown to the localhost bridge, and the bridge performs the authenticated Workflowy write. If ChatGPT changes its DOM attributes, update `content.js`; the account-wide JSON importer (`wf chatgpt`) remains the stable fallback.
