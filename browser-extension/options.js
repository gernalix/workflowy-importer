const input = document.getElementById('token');
const status = document.getElementById('status');
document.getElementById('pairing').addEventListener('submit', async event => {
  event.preventDefault();
  const token = input.value;
  input.value = '';
  if (!WorkflowyCapture.validToken(token)) {
    status.textContent = 'Use the dedicated random capture key (43–128 base64url characters).';
    return;
  }
  try {
    await chrome.storage.session.setAccessLevel({accessLevel: 'TRUSTED_CONTEXTS'});
    await chrome.storage.session.set({captureToken: token});
    status.textContent = 'Paired for this browser session.';
  } catch {
    status.textContent = 'Pairing could not be saved.';
  }
});
document.getElementById('forget').addEventListener('click', async () => {
  input.value = '';
  await chrome.storage.session.remove('captureToken');
  status.textContent = 'Pairing forgotten.';
});
