// Settings → Appearance → Navigation Bar editor.
// Controlled component: never copies `layout`; every mutation builds a new
// {v,bar,more,hidden} and calls onChange(next). The parent owns persistence
// and the "Saved" pulse. Reordering works via native HTML5 drag-and-drop
// (desktop) AND the arrow buttons (touch fallback — touch never fires DnD).
import React, { useState } from "react";
import { NAV_ITEM_MAP, resolveNavLayout } from "../navItems.js";

const ZONES = [
  ["bar", "Main bar"],
  ["more", 'Behind "More"'],
  ["hidden", "Hidden"],
];
const ZONE_ORDER = ["bar", "more", "hidden"];
const ZONE_LABEL = Object.fromEntries(ZONES);

function moveItem(layout, id, from, to, toIndex) {
  const next = { v: 1, bar: [...layout.bar], more: [...layout.more], hidden: [...layout.hidden] };
  const fromIdx = next[from].indexOf(id);
  if (fromIdx < 0) return layout;
  next[from].splice(fromIdx, 1);
  let idx = toIndex == null ? next[to].length : toIndex;
  if (from === to && fromIdx < idx) idx -= 1;
  idx = Math.max(0, Math.min(idx, next[to].length));
  next[to].splice(idx, 0, id);
  return next;
}

export default function NavLayoutEditor({ t, layout, onChange, isMobile }) {
  const [drag, setDrag] = useState(null); // {id, from}
  const [dropHint, setDropHint] = useState(null); // {zone, index}
  const lastBarItem = layout.bar.length === 1;
  const canLeave = (from) => !(from === "bar" && lastBarItem);
  const isDefault = JSON.stringify(layout) === JSON.stringify(resolveNavLayout(null));

  const clearDnd = () => { setDrag(null); setDropHint(null); };
  const applyMove = (id, from, to, toIndex) => {
    if (from !== to && !canLeave(from)) return;
    onChange(moveItem(layout, id, from, to, toIndex));
  };

  const onZoneDragOver = (zone) => (e) => {
    if (!drag) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    const rows = [...e.currentTarget.querySelectorAll("[data-nav-row]")];
    let index = rows.length;
    for (let i = 0; i < rows.length; i++) {
      const r = rows[i].getBoundingClientRect();
      if (e.clientY < r.top + r.height / 2) { index = i; break; }
    }
    setDropHint((p) => (p && p.zone === zone && p.index === index ? p : { zone, index }));
  };
  const onZoneDrop = (zone) => (e) => {
    e.preventDefault();
    if (drag) applyMove(drag.id, drag.from, zone, dropHint && dropHint.zone === zone ? dropHint.index : undefined);
    clearDnd();
  };
  const onZoneDragLeave = (e) => {
    if (!e.currentTarget.contains(e.relatedTarget)) setDropHint(null);
  };

  const miniBtnS = (disabled) => ({
    width: 22, height: 22, display: "flex", alignItems: "center", justifyContent: "center",
    background: "transparent", border: `1px solid ${t.brd}44`, borderRadius: 5, color: t.mut,
    fontSize: 11, lineHeight: 1, cursor: disabled ? "default" : "pointer", padding: 0,
    opacity: disabled ? 0.3 : 1, pointerEvents: disabled ? "none" : "auto", flexShrink: 0,
  });
  const indicator = <div style={{ height: 2, alignSelf: "stretch", background: t.acc, borderRadius: 1, margin: "1px 2px", flexShrink: 0 }} />;

  const row = (id, zone, index, count) => {
    const it = NAV_ITEM_MAP[id];
    if (!it) return null;
    const zi = ZONE_ORDER.indexOf(zone);
    const prevZone = zi > 0 ? ZONE_ORDER[zi - 1] : null;
    const nextZone = zi < ZONE_ORDER.length - 1 ? ZONE_ORDER[zi + 1] : null;
    return (
      <div key={id} data-nav-row draggable
        onDragStart={(e) => { e.dataTransfer.setData("text/plain", id); e.dataTransfer.effectAllowed = "move"; setDrag({ id, from: zone }); }}
        onDragEnd={clearDnd}
        style={{ display: "flex", alignItems: "center", gap: 7, padding: "5px 8px", background: `${t.surface}66`, border: `1px solid ${t.brd}33`, borderRadius: 7, cursor: "grab", fontSize: 12, color: t.text, opacity: drag && drag.id === id ? 0.4 : 1, userSelect: "none" }}>
        <span style={{ color: t.mut, fontSize: 10, letterSpacing: -1, cursor: "grab" }}>⋮⋮</span>
        <span style={{ display: "flex", alignItems: "center", color: t.dim, fontSize: 14 }}><it.icon /></span>
        <span style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{it.label}</span>
        <button title="Move up" onClick={() => applyMove(id, zone, zone, index - 1)} style={miniBtnS(index === 0)}>↑</button>
        <button title="Move down" onClick={() => applyMove(id, zone, zone, index + 2)} style={miniBtnS(index === count - 1)}>↓</button>
        <button title={prevZone ? `Move to ${ZONE_LABEL[prevZone]}` : ""} onClick={() => prevZone && applyMove(id, zone, prevZone)} style={miniBtnS(!prevZone)}>←</button>
        <button title={nextZone ? `Move to ${ZONE_LABEL[nextZone]}` : ""} onClick={() => nextZone && applyMove(id, zone, nextZone)} style={miniBtnS(!nextZone || !canLeave(zone))}>→</button>
      </div>
    );
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <div style={{ display: "grid", gridTemplateColumns: isMobile ? "1fr" : "1fr 1fr", gap: 10 }}>
        {ZONES.map(([zone, title]) => {
          const ids = layout[zone];
          const hintHere = dropHint && dropHint.zone === zone && drag;
          return (
            <div key={zone} style={zone === "hidden" ? { gridColumn: "1 / -1" } : undefined}>
              <div style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: 1, color: t.mut, fontWeight: 700, marginBottom: 5 }}>{title}</div>
              <div onDragOver={onZoneDragOver(zone)} onDrop={onZoneDrop(zone)} onDragLeave={onZoneDragLeave}
                style={{ background: `${t.bgDeep}70`, border: `1px solid ${t.brd}24`, borderRadius: 8, padding: 8, display: "flex", flexDirection: "column", gap: 4, minHeight: 52 }}>
                {ids.map((id, i) => (
                  <React.Fragment key={id}>
                    {hintHere && dropHint.index === i && indicator}
                    {row(id, zone, i, ids.length)}
                  </React.Fragment>
                ))}
                {hintHere && dropHint.index === ids.length && indicator}
                {!ids.length && <div style={{ border: `1px dashed ${t.brd}55`, borderRadius: 7, padding: "10px 0", textAlign: "center", fontSize: 10, color: t.mut }}>Drag items here</div>}
              </div>
            </div>
          );
        })}
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <button onClick={() => onChange(resolveNavLayout(null))} disabled={isDefault}
          style={{ padding: "6px 10px", background: `${t.err}14`, border: `1px solid ${t.err}33`, borderRadius: 7, color: t.err, fontSize: 10, cursor: isDefault ? "default" : "pointer", fontWeight: 700, whiteSpace: "nowrap", opacity: isDefault ? 0.4 : 1 }}>
          Reset to defaults
        </button>
        <div style={{ fontSize: 10, color: t.mut }}>At least one item must stay on the main bar.</div>
      </div>
    </div>
  );
}
