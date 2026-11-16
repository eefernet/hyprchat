// The message that replaces a user message being edited and sent again.
//
// A message has two texts: what the bubble shows (`content`) and what the model was given
// (`_fullContent`, saved as `metadata.model_content`): the same text plus the extracted text of attached
// PDFs and a line naming attached images. Editing rebuilt the message from the new text and the
// attachment chips only, so the bubble still showed the PDF while the model no longer had it.

// What the model was given beyond what the bubble shows.
export function attachmentText(original) {
  const shown = String(original?.content || '');
  const full = String(original?._fullContent || original?.metadata?.model_content || '');
  if (!full || full === shown) return '';
  if (!shown) return `\n\n${full}`;
  return full.startsWith(shown) ? full.slice(shown.length) : '';
}

export function editedMessage(original, text) {
  const extra = attachmentText(original);
  return {
    role: 'user',
    content: text,
    ...(extra.trim() ? { _fullContent: `${text}${extra}` } : {}),
    ...(original?.metadata ? { metadata: original.metadata } : {}),
    ...(Array.isArray(original?._images) && original._images.length ? { _images: original._images } : {}),
  };
}
