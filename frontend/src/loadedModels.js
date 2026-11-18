// Wording and numbers for Settings → Model & Generation → "Models loaded at once" (backend
// loaded_models.py). Pure, so node:test can import it.
import { formatSize } from './storageSummary.js';

export const LIMIT_OFF = 0;
export const LIMIT_CHOICES = [0, 1, 2, 3, 4, 6, 8];

// The stored value as one of the choices: anything unreadable is off, anything between two choices the lower.
export function limitChoice(value) {
  const number = Math.floor(Number(value));
  if (!Number.isFinite(number) || number <= 0) return LIMIT_OFF;
  return LIMIT_CHOICES.filter(choice => choice <= number).pop() ?? LIMIT_OFF;
}

export const limitLabel = value => (value === LIMIT_OFF ? 'Ollama decides' : String(value));

export function limitLine(limit) {
  if (!limit) return 'Ollama keeps models loaded while they fit in memory, up to its own limit.';
  if (limit === 1) return 'Before a chat loads its model, HyprChat unloads the other one. The model gets the memory to itself.';
  return `When ${limit} models are loaded and a chat needs another, HyprChat first unloads the one used longest ago.`;
}

function minutesLeft(expiresAt, now) {
  const when = Date.parse(String(expiresAt || '').replace(/(\.\d{3})\d+/, '$1'));
  if (!Number.isFinite(when)) return null;
  return Math.max(0, Math.round((when - now) / 60000));
}

// One loaded model as the list shows it.
export function modelRow(row, now = Date.now()) {
  const size = Number(row?.size_bytes) || 0;
  const onGpu = Math.min(Number(row?.gpu_bytes) || 0, size);
  const offGpu = size > 0 ? Math.round(((size - onGpu) / size) * 100) : 0;
  const details = [];
  if (size) details.push(formatSize(size));
  if (Number(row?.context) > 0) details.push(`${Number(row.context).toLocaleString('en-US')} context`);
  const minutes = minutesLeft(row?.expires_at, now);
  if (minutes != null && minutes < 24 * 60) details.push(minutes < 1 ? 'unloads soon' : `unloads in ${minutes} min if unused`);
  return {
    name: String(row?.name || ''),
    helper: !!row?.helper,
    detail: details.join(' · '),
    // Part of a model in system memory instead of on the GPU makes it many times slower.
    spill: size > 0 && offGpu >= 1 ? `${offGpu}% not on the GPU` : '',
  };
}

// The line above the list.
export function loadedLine(status) {
  if (!status) return 'Checking…';
  if (status.reachable === false) return 'Ollama could not be reached.';
  const models = status.models || [];
  if (!models.length) return 'Nothing is loaded right now.';
  const counted = models.filter(row => !row.helper).length;
  const helpers = models.length - counted;
  const parts = [`${counted} model${counted === 1 ? '' : 's'} loaded`];
  if (helpers) parts.push(`${helpers} helper${helpers === 1 ? '' : 's'}`);
  return parts.join(' · ');
}
