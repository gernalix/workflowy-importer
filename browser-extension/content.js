function cleanText(el) {
  return (el.innerText || el.textContent || "").trim();
}

function exportCurrentChat() {
  const nodes = [...document.querySelectorAll('[data-message-author-role]')];
  if (!nodes.length) {
    throw new Error('No ChatGPT messages found on this page');
  }
  const blocks = [];
  for (const node of nodes) {
    const role = node.getAttribute('data-message-author-role');
    if (!['user', 'assistant'].includes(role)) continue;
    const text = cleanText(node);
    if (!text) continue;
    blocks.push(`## ${role === 'user' ? 'User' : 'Assistant'}\n\n${text}`);
  }
  return {
    title: document.title.replace(/\s*[-–]\s*ChatGPT\s*$/i, '').trim() || 'ChatGPT conversation',
    source_url: location.href,
    markdown: blocks.join('\n\n')
  };
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type !== 'export-chat') return;
  try {
    sendResponse({ok: true, payload: exportCurrentChat()});
  } catch (error) {
    sendResponse({ok: false, error: String(error)});
  }
});
