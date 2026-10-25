// Nav rail registry + per-user layout resolution — JSX-free on purpose so
// node:test can import it. Icons are component NAMES (keys of IC in
// components/icons.jsx); navItems.js binds them to the real components.
export const NAV_LAYOUT_VERSION = 2;

// Ordered registry of the customizable panel buttons. id === the `panel`
// state value in main.jsx. Registry order IS the default order.
export const NAV_REGISTRY = [
  { id: "chat", label: "Chat", short: "Chat", icon: "Chat" },
  { id: "workspaces", label: "Workspaces", short: "Projects", icon: "Folder" },
  { id: "library", label: "Library Search", short: "Search", icon: "Search" },
  { id: "research", label: "Deep Research", short: "Research", icon: "Research" },
  { id: "council", label: "Council of AI", short: "Council", icon: "Council" },
  { id: "personas", label: "Agents", short: "Agents", icon: "Cube" },
  { id: "images", label: "Image Studio", short: "Image Gen", icon: "Image" },
  { id: "assistant", label: "Assistant", short: "Assistant", icon: "Bot" },
  { id: "tasks", label: "Tasks", short: "Tasks", icon: "Clock" },
  { id: "jobs", label: "Jobs", short: "Jobs", icon: "Activity" },
  { id: "calendar", label: "Calendar", short: "Calendar", icon: "Calendar" },
  { id: "notes", label: "Notes", short: "Notes", icon: "Pencil" },
  { id: "email", label: "Email", short: "Email", icon: "Mail" },
  { id: "artifacts", label: "Artifacts", short: "Artifacts", icon: "Package" },
  { id: "memory", label: "Memory", short: "Memory", icon: "Brain" },
  { id: "prompts", label: "Prompt Library", short: "Prompts", icon: "Zap" },
  { id: "kb", label: "Knowledge Bases", short: "KB", icon: "Database" },
  { id: "tools", label: "Tools", short: "Tools", icon: "Tool" },
  { id: "models", label: "Model Manager", short: "Models", icon: "HardDrive" },
  { id: "analytics", label: "Analytics", short: "Stats", icon: "BarChart" },
];

export const NAV_ITEM_IDS = NAV_REGISTRY.map((i) => i.id);
const REGISTRY_MAP = Object.fromEntries(NAV_REGISTRY.map((i) => [i.id, i]));

export const DEFAULT_NAV_LAYOUT = {
  v: NAV_LAYOUT_VERSION,
  bar: ["chat", "workspaces", "research", "council", "personas", "images"],
  more: ["library", "assistant", "tasks", "jobs", "calendar", "notes", "email", "artifacts", "memory", "prompts", "kb", "tools", "models", "analytics"],
  hidden: [],
};

const ZONE_KEYS = ["bar", "more", "hidden"];

export const defaultZoneOf = (id) => (DEFAULT_NAV_LAYOUT.bar.includes(id) ? "bar" : "more");

// Merge a saved nav-layout pref against the registry into a fresh canonical
// {v, bar, more, hidden}. Key order is deterministic on purpose — hydration
// compares layouts via JSON.stringify, so a stable shape avoids spurious PUTs.
// Idempotent: resolveNavLayout(resolveNavLayout(x)) deep-equals resolveNavLayout(x).
// Layouts saved before v2 (no `v`, or v < 2) get the one-time migration that
// promotes "workspaces" from "more" onto the bar right after "chat"; the output
// carries v:2 so the promotion never repeats. Hidden stays hidden.
export function resolveNavLayout(saved) {
  const fresh = () => ({
    v: NAV_LAYOUT_VERSION,
    bar: [...DEFAULT_NAV_LAYOUT.bar],
    more: [...DEFAULT_NAV_LAYOUT.more],
    hidden: [],
  });
  if (!saved || typeof saved !== "object" || Array.isArray(saved)) return fresh();
  if (ZONE_KEYS.some((z) => !Array.isArray(saved[z]))) return fresh();
  const seen = new Set();
  const out = { v: NAV_LAYOUT_VERSION, bar: [], more: [], hidden: [] };
  for (const z of ZONE_KEYS) {
    for (const id of saved[z]) {
      if (typeof id !== "string" || !REGISTRY_MAP[id] || seen.has(id)) continue;
      seen.add(id);
      out[z].push(id);
    }
  }
  // Items added after the pref was saved land at the end of their default
  // zone — never hidden, so new features stay discoverable.
  for (const it of NAV_REGISTRY) {
    if (!seen.has(it.id)) out[defaultZoneOf(it.id)].push(it.id);
  }
  const savedV = typeof saved.v === "number" ? saved.v : 1;
  if (savedV < 2) {
    const i = out.more.indexOf("workspaces");
    if (i >= 0) {
      out.more.splice(i, 1);
      const c = out.bar.indexOf("chat");
      out.bar.splice(c >= 0 ? c + 1 : 0, 0, "workspaces");
    }
  }
  // ≥1 item must stay on the bar; every registry id is in some zone by now.
  if (!out.bar.length) out.bar.push(out.more.shift() ?? out.hidden.shift());
  return out;
}
