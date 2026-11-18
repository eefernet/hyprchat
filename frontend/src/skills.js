// Pure helpers for the Skills panel — JSX-free so node:test can import them.

export const ORIGIN_LABELS = { user: 'Written by you', agent: 'Saved in chat', reviewer: 'Learned', import: 'Imported' };
export const STATUS_LABELS = { active: 'Active', draft: 'Needs review', stale: 'Unused lately', archived: 'Archived' };
export const FLAG_LABELS = {
  override: 'tries to override instructions', exfiltration: 'sends data to a URL', credentials: 'reads credentials',
  email: 'sends email', encoded: 'contains an encoded blob',
};
export const TABS = [{ id: 'library', label: 'Library' }, { id: 'review', label: 'Review' }, { id: 'archived', label: 'Archived' }];

// A skill waits on the owner when it is a draft, or when a change to it was parked.
export const needsReview = skill => skill.status === 'draft' || !!skill.pending;

export const reviewCount = skills => skills.filter(needsReview).length;

export function skillsForTab(skills, tab, query = '') {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  const matches = skill => words.every(word => `${skill.name} ${skill.description} ${skill.category}`.toLowerCase().includes(word));
  const inTab = skill => tab === 'review' ? needsReview(skill)
    : tab === 'archived' ? skill.status === 'archived'
    : skill.status === 'active' || skill.status === 'stale';
  return skills.filter(skill => inTab(skill) && matches(skill))
    .sort((a, b) => (b.pinned - a.pinned) || a.name.localeCompare(b.name));
}

export function usageLabel(skill) {
  if (!skill.use_count) return 'Not used yet';
  return `Used ${skill.use_count} time${skill.use_count === 1 ? '' : 's'}`;
}

// Why a skill or a change is waiting, in the owner's words.
export function heldLabel(skill) {
  const reason = skill.pending?.held || '';
  if (reason) return reason.replace(/^./, c => c.toUpperCase());
  const flags = (skill.scan_flags || []).map(flag => FLAG_LABELS[flag] || flag);
  if (flags.length) return `Safety check: ${flags.join(', ')}`;
  return skill.origin === 'import' ? 'Imported' : 'Waiting for your approval';
}

// Line diff (longest common subsequence) for reviewing a proposed change. Skill bodies are capped at
// 6,000 characters, so the quadratic table stays small.
export function lineDiff(before = '', after = '') {
  const a = before.split('\n'), b = after.split('\n');
  const table = Array.from({ length: a.length + 1 }, () => new Uint16Array(b.length + 1));
  for (let i = a.length - 1; i >= 0; i--) {
    for (let j = b.length - 1; j >= 0; j--) {
      table[i][j] = a[i] === b[j] ? table[i + 1][j + 1] + 1 : Math.max(table[i + 1][j], table[i][j + 1]);
    }
  }
  const rows = [];
  let i = 0, j = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) { rows.push({ type: 'same', text: a[i] }); i++; j++; }
    else if (table[i + 1][j] >= table[i][j + 1]) rows.push({ type: 'del', text: a[i++] });
    else rows.push({ type: 'add', text: b[j++] });
  }
  while (i < a.length) rows.push({ type: 'del', text: a[i++] });
  while (j < b.length) rows.push({ type: 'add', text: b[j++] });
  return rows;
}

export const diffStats = rows => ({
  added: rows.filter(row => row.type === 'add').length,
  removed: rows.filter(row => row.type === 'del').length,
});

export const slugify = text => String(text || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 48);

export const EMPTY_SKILL = { name: '', description: '', category: '', body: '## When to use\n\n\n## Steps\n1. \n\n## Pitfalls\n- ' };
