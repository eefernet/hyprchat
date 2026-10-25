// Browser tab title. JSX-free so node:test can import it.
// "● " marks an in-flight generation; the page title (chat title, workspace
// name or panel label) precedes the app name when present.
export function formatDocumentTitle({title, streaming} = {}) {
  const text = title == null ? '' : String(title).trim();
  return `${streaming ? '● ' : ''}${text ? text + ' · ' : ''}HyprChat`;
}
