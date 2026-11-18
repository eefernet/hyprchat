// Wording and numbers for Settings → Storage & Cleanup. Pure, so node:test can import it.

export function formatSize(bytes) {
  if (bytes == null || Number.isNaN(Number(bytes))) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const value = Number(bytes);
  const i = value > 0 ? Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1) : 0;
  const scaled = value / 1024 ** i;
  return `${i ? scaled.toFixed(scaled >= 100 ? 0 : 1) : value} ${units[i]}`;
}

const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
export function monthLabel(month) {
  const match = String(month || '').match(/^(\d{4})-(\d{2})$/);
  return match && MONTHS[Number(match[2]) - 1] ? `${MONTHS[Number(match[2]) - 1]} ${match[1]}` : String(month || 'Other');
}

const count = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

// What the "Files on HyprChat" card shows. `preview` is /api/settings/cleanup-preview, whose `outputs`
// counts every delivered file by month and chat. The page used to read the cleanup's own top-level
// count, which is 0 once files live in folders. `info` (/api/settings) is the fallback for an older server.
export function localFilesView(preview, info) {
  const outputs = preview?.outputs || null;
  const files = outputs ? outputs.files : info?.sandbox_file_count ?? null;
  const bytes = outputs ? outputs.bytes : info?.sandbox_size_bytes ?? null;
  const kinds = outputs?.kinds || {};
  const where = [
    ['Chats', kinds.chats], ['Image Studio', kinds.image_studio], ['Library', kinds.library], ['Unsorted', kinds.unsorted], ['Not in a folder', outputs?.loose],
  ].filter(([, kind]) => kind && kind.files > 0).map(([label, kind]) => ({label, files: kind.files, size: formatSize(kind.bytes)}));
  const months = (outputs?.months || []).map(m => ({key: m.month, label: monthLabel(m.month), files: m.files, size: formatSize(m.bytes), chats: m.chats}));
  const reclaimFiles = preview?.reclaimable_files || 0;
  return {
    known: files != null,
    headline: files == null ? 'Not loaded' : `${count(files, 'file')} · ${formatSize(bytes)}`,
    folders: outputs ? outputs.chat_folders : null,
    where, months,
    canClean: reclaimFiles > 0,
    cleanLine: !preview ? '' : reclaimFiles > 0
      ? `${count(reclaimFiles, 'temporary file')} (${formatSize(preview.reclaimable_bytes)}) can be removed.`
      : 'Nothing to clean right now.',
  };
}

// How the sandbox VM's automatic cleanup is described, in one line.
export const VM_AUTO_CLEANUP = 'Cleans itself every 10 minutes: finished tests after a day, unused caches after a week. Work in progress is never touched.';
