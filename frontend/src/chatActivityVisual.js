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

// Original Lantern word pools; presentation only, never activity state.
export const ACTIVITY_VERBS = {
  "writing": [
    "Coagulating",
    "Composing",
    "Conjuring",
    "Weaving",
    "Spinning",
    "Stitching",
    "Drafting",
    "Inking",
    "Scribbling",
    "Penning",
    "Typesetting",
    "Transcribing",
    "Narrating",
    "Articulating",
    "Phrasing",
    "Wordsmithing",
    "Crafting",
    "Forging",
    "Smithing",
    "Hammering",
    "Sculpting",
    "Chiseling",
    "Carving",
    "Whittling",
    "Kneading",
    "Baking",
    "Plating",
    "Garnishing",
    "Seasoning",
    "Simmering",
    "Reducing",
    "Glazing",
    "Frosting",
    "Drizzling",
    "Pouring",
    "Decanting",
    "Bottling",
    "Distilling",
    "Condensing",
    "Crystallizing",
    "Precipitating",
    "Manifesting",
    "Materializing",
    "Rendering",
    "Unspooling",
    "Unfurling",
    "Blooming",
    "Sprouting",
    "Germinating",
    "Assembling",
    "Compiling",
    "Linking",
    "Emitting",
    "Streaming",
    "Broadcasting",
    "Transmitting",
    "Beaming",
    "Dispatching",
    "Serenading",
    "Orchestrating"
  ],
  "thinking": [
    "Cogitating",
    "Pondering",
    "Ruminating",
    "Musing",
    "Mulling",
    "Deliberating",
    "Contemplating",
    "Percolating",
    "Marinating",
    "Brewing",
    "Steeping",
    "Fermenting",
    "Incubating",
    "Hatching",
    "Gestating",
    "Noodling",
    "Doodling",
    "Puzzling",
    "Untangling",
    "Unknotting",
    "Decoding",
    "Deciphering",
    "Triangulating",
    "Calibrating",
    "Computing",
    "Calculating",
    "Reckoning",
    "Weighing",
    "Sifting",
    "Winnowing",
    "Sorting",
    "Scheming",
    "Plotting",
    "Strategizing",
    "Hypothesizing",
    "Theorizing",
    "Speculating",
    "Extrapolating",
    "Inferring",
    "Deducing",
    "Synthesizing",
    "Coalescing",
    "Converging",
    "Resonating",
    "Vibing",
    "Meditating",
    "Levitating",
    "Channeling",
    "Divining",
    "Scrying",
    "Dreaming",
    "Daydreaming",
    "Wondering",
    "Squinting",
    "Furrowing",
    "Scratching Chin",
    "Consulting Stars",
    "Reading Tea Leaves",
    "Spelunking",
    "Connecting Dots"
  ],
  "searching": [
    "Scouring",
    "Sleuthing",
    "Prowling",
    "Foraging",
    "Trawling",
    "Combing",
    "Rummaging",
    "Snooping",
    "Sniffing",
    "Questing",
    "Canvassing",
    "Surveying",
    "Scanning",
    "Probing",
    "Fishing",
    "Dowsing",
    "Hunting",
    "Beachcombing"
  ],
  "tool": [
    "Cranking",
    "Tinkering",
    "Wrenching",
    "Soldering",
    "Welding",
    "Fiddling",
    "Jiggering",
    "Rigging",
    "Greasing",
    "Executing",
    "Crunching",
    "Churning",
    "Grinding",
    "Buzzing",
    "Whirring",
    "Clanking",
    "Pedaling",
    "Crank-starting"
  ],
  "starting": [
    "Warming Up",
    "Stretching",
    "Booting",
    "Igniting",
    "Kindling",
    "Sparking",
    "Summoning",
    "Awakening",
    "Stirring",
    "Rousing",
    "Limbering",
    "Tuning"
  ]
};

export function playfulPhase(label,phase) {
  const normalized=String(label).toLowerCase();
  const defaults={starting:['starting'],thinking:['thinking'],writing:['writing','streaming'],searching:['searching'],tool:['tool','tool running']};
  const key=phase==='streaming'?'writing':phase;
  return defaults[key]?.includes(normalized)?key:null;
}
export function activityVerbOrder(phase,seed) {
  const words=[...(ACTIVITY_VERBS[phase]||[])];
  let hash=2166136261;
  for(const c of `${seed}:${phase}`)hash=Math.imul(hash^c.charCodeAt(0),16777619)>>>0;
  for(let i=words.length-1;i>0;i--){
    hash=(Math.imul(hash,1664525)+1013904223)>>>0;
    const j=hash%(i+1);[words[i],words[j]]=[words[j],words[i]];
  }
  return words;
}
export function activityDisplayLabel(state,seed,tick=0,enabled=true) {
  const phase=playfulPhase(state.label,state.phase);
  if(!enabled||!state.active||!phase)return state.label;
  const words=activityVerbOrder(phase,seed);
  return words[Math.max(0,Math.floor(tick))%words.length]||state.label;
}
