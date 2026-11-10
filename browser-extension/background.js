importScripts('capture-protocol.js');

chrome.action.onClicked.addListener(async (tab) => {
  if (!tab.id || !tab.url?.startsWith('https://chatgpt.com/')) return;
  try {
    await chrome.storage.session.setAccessLevel({accessLevel: 'TRUSTED_CONTEXTS'});
    const {captureToken} = await chrome.storage.session.get('captureToken');
    if (!WorkflowyCapture.validToken(captureToken)) {
      await chrome.runtime.openOptionsPage();
      await chrome.action.setBadgeText({tabId: tab.id, text: '?'});
      return;
    }
    await WorkflowyCapture.capture(captureToken, async () => {
      const response = await chrome.tabs.sendMessage(tab.id, {type: 'export-chat'});
      if (!response?.ok) throw new Error('Could not read the conversation');
      return response.payload;
    });
    await chrome.action.setBadgeText({tabId: tab.id, text: '✓'});
    setTimeout(() => chrome.action.setBadgeText({tabId: tab.id, text: ''}), 1800);
  } catch {
    await chrome.action.setBadgeText({tabId: tab.id, text: '!'});
    console.error('ChatGPT to Workflowy: capture failed; check the destination before retrying.');
  }
});
