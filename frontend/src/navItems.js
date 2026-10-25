// Nav rail item registry + per-user layout resolution.
// Icon values are component REFERENCES (IC.Chat), never elements — this is a
// .js module (no JSX transform); render via <it.icon/> at the call site.
// The JSX-free registry (icon NAMES) and the layout logic live in navLayout.js
// so node:test can import them; this module only binds the icon components.
import { IC } from "./components/icons.jsx";
import { NAV_REGISTRY } from "./navLayout.js";

export { DEFAULT_NAV_LAYOUT, resolveNavLayout, NAV_LAYOUT_VERSION } from "./navLayout.js";

export const NAV_ITEMS = NAV_REGISTRY.map((it) => ({ ...it, icon: IC[it.icon] }));

export const NAV_ITEM_MAP = Object.fromEntries(NAV_ITEMS.map((i) => [i.id, i]));
