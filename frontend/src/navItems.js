// Nav rail item registry + per-user layout resolution.
// Icon values are component REFERENCES (IC.Chat), never elements — this is a
// .js module (no JSX transform); render via <it.icon/> at the call site.
import { IC } from "./components/icons.jsx";

// Ordered registry of the customizable panel buttons. id === the `panel`
// state value in main.jsx. Registry order IS the default order.
export const NAV_ITEMS = [
  { id: "chat", label: "Chat", short: "Chat", icon: IC.Chat },
  { id: "research", label: "Deep Research", short: "Research", icon: IC.Research },
  { id: "council", label: "Council of AI", short: "Council", icon: IC.Council },
  { id: "personas", label: "Agents", short: "Agents", icon: IC.Cube },
  { id: "images", label: "Image Studio", short: "Image Gen", icon: IC.Image },
  { id: "assistant", label: "Assistant", short: "Assistant", icon: IC.Bot },
  { id: "tasks", label: "Tasks", short: "Tasks", icon: IC.Clock },
  { id: "calendar", label: "Calendar", short: "Calendar", icon: IC.Calendar },
  { id: "notes", label: "Notes", short: "Notes", icon: IC.Pencil },
  { id: "email", label: "Email", short: "Email", icon: IC.Send },
  { id: "artifacts", label: "Artifacts", short: "Artifacts", icon: IC.Layers },
  { id: "memory", label: "Memory", short: "Memory", icon: IC.Brain },
  { id: "prompts", label: "Prompt Library", short: "Prompts", icon: IC.Zap },
  { id: "kb", label: "Knowledge Bases", short: "KB", icon: IC.Database },
  { id: "tools", label: "Tools", short: "Tools", icon: IC.Tool },
  { id: "models", label: "Model Manager", short: "Models", icon: IC.HardDrive },
  { id: "analytics", label: "Analytics", short: "Stats", icon: IC.BarChart },
];

export const NAV_ITEM_MAP = Object.fromEntries(NAV_ITEMS.map((i) => [i.id, i]));

export const DEFAULT_NAV_LAYOUT = {
  v: 1,
  bar: ["chat", "research", "council", "personas", "images"],
  more: ["assistant", "tasks", "calendar", "notes", "email", "artifacts", "memory", "prompts", "kb", "tools", "models", "analytics"],
  hidden: [],
};

const ZONE_KEYS = ["bar", "more", "hidden"];

const defaultZoneOf = (id) => (DEFAULT_NAV_LAYOUT.bar.includes(id) ? "bar" : "more");

// Merge a saved nav-layout pref against the registry into a fresh canonical
// {v, bar, more, hidden}. Key order is deterministic on purpose — hydration
// compares layouts via JSON.stringify, so a stable shape avoids spurious PUTs.
// Idempotent: resolveNavLayout(resolveNavLayout(x)) deep-equals resolveNavLayout(x).
export function resolveNavLayout(saved) {
  const fresh = () => ({
    v: 1,
    bar: [...DEFAULT_NAV_LAYOUT.bar],
    more: [...DEFAULT_NAV_LAYOUT.more],
    hidden: [],
  });
  if (!saved || typeof saved !== "object" || Array.isArray(saved)) return fresh();
  if (ZONE_KEYS.some((z) => !Array.isArray(saved[z]))) return fresh();
  const seen = new Set();
  const out = { v: 1, bar: [], more: [], hidden: [] };
  for (const z of ZONE_KEYS) {
    for (const id of saved[z]) {
      if (typeof id !== "string" || !NAV_ITEM_MAP[id] || seen.has(id)) continue;
      seen.add(id);
      out[z].push(id);
    }
  }
  // Items added after the pref was saved land at the end of their default
  // zone — never hidden, so new features stay discoverable.
  for (const it of NAV_ITEMS) {
    if (!seen.has(it.id)) out[defaultZoneOf(it.id)].push(it.id);
  }
  // ≥1 item must stay on the bar; every registry id is in some zone by now.
  if (!out.bar.length) out.bar.push(out.more.shift() ?? out.hidden.shift());
  return out;
}
