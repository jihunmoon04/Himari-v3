import { renderAssistantText } from './assets/rich_text.js';
window.chrome.webview.addEventListener('message', ({ data }) => {
  if (!data || typeof data.text !== 'string') return;
  document.getElementById('answer').replaceChildren(renderAssistantText(document, data.text));
});
window.chrome.webview.postMessage('ready');
