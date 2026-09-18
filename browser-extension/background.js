chrome.action.onClicked.addListener(async (tab) => {
  if (!tab.id || !tab.url?.startsWith('https://chatgpt.com/')) return;
  try {
    const response = await chrome.tabs.sendMessage(tab.id, {type: 'export-chat'});
    if (!response?.ok) throw new Error(response?.error || 'Could not read the conversation');
    const bridge = await fetch('http://127.0.0.1:8765/capture', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(response.payload)
    });
    if (!bridge.ok) throw new Error(`Workflowy bridge returned HTTP ${bridge.status}`);
    await bridge.json();
    await chrome.action.setBadgeText({tabId: tab.id, text: '✓'});
    setTimeout(() => chrome.action.setBadgeText({tabId: tab.id, text: ''}), 1800);
  } catch (error) {
    await chrome.action.setBadgeText({tabId: tab.id, text: '!'});
    console.error('ChatGPT to Workflowy:', error);
  }
});
