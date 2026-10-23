// Presentation mapping for the chat Activity pill: phase/tool -> icon name
// (components/icons.jsx IC) and theme colour. Pure, so it is unit-tested;
// the state machine itself lives in chatActivity.js.
export const PHASE_ICONS = {starting:'Zap', thinking:'Brain', writing:'Pencil', reviewing:'Star', searching:'Search', tool:'Tool', complete:'Check', stopped:'Stop', failed:'Alert'};
export const TOOL_ICONS = {fetch_url:'Globe', execute_code:'Terminal', run_shell:'Terminal', read_file:'File', write_file:'Pencil', list_files:'Layers', generate_image:'Image', kb:'Database', memory:'Brain', download_file:'Download', download_project:'Download', install_package:'Cube', deep_research:'Research'};

export function activityColor(state, t) {
  if (state.phase === 'failed') return t.err;
  if (state.phase === 'thinking') return t.f4;
  if (state.phase === 'reviewing') return t.warm;
  if (state.phase === 'stopped') return t.mut;
  return state.active ? t.acc : t.dim;
}

export function activityIcon(state, tool) {
  return (state.phase === 'tool' && TOOL_ICONS[tool]) || PHASE_ICONS[state.phase] || 'Activity';
}

export function activityVisual(state, t, tool) {
  const color = activityColor(state, t);
  return {
    color,
    icon: activityIcon(state, tool),
    vars: {
      '--activity-color': color, '--activity-ring': `${color}40`, '--activity-tint': `${color}0A`,
      '--activity-tint-strong': `${color}1F`, '--activity-glow': `${color}14`,
      '--activity-surface': 'var(--hc-elev-1)', '--activity-muted': t.mut, '--activity-text': t.dim,
    },
  };
}

export const summaryChips = state => [
  state.entries.some(e => e.kind === 'reasoning') ? 'Reasoning' : '',
  state.tools ? `${state.tools} tool${state.tools === 1 ? '' : 's'}` : '',
  state.entries.some(e => e.kind === 'review') ? 'Review' : '',
].filter(Boolean);
