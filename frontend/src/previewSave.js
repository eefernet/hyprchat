// Saving what the preview pane shows as a file of its chat (backend/routes/chat_saves.py).
import {API} from './session.js';

// Chats that exist only in this tab have nowhere to save to.
export const canSaveTo = conversationId => !!conversationId && !/^(ghost|l)-/.test(String(conversationId));

export async function savePreview({conversationId, filename, content, kind}) {
  const response = await fetch(`${API}/api/conversations/${encodeURIComponent(conversationId)}/saved-previews`, {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({filename, content, kind}),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `The server answered ${response.status}`);
  return data;   // {artifact, already_saved}
}

export function downloadText(name, text, type = 'text/plain') {
  const url = URL.createObjectURL(new Blob([text], {type}));
  const link = document.createElement('a'); link.href = url; link.download = name;
  document.body.appendChild(link); link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
