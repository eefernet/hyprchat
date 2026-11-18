import React, { useId, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { compactionLimit, forDisplay, formatTokens, meterView } from '../contextMeter.js';
import useAnchoredPopover from '../useAnchoredPopover.js';
import useFocusScope from '../useFocusScope.js';
import usePresence from '../usePresence.js';
import ContextBreakdown from './ContextBreakdown.jsx';
import './contextMeter.css';

// The context pill in the chat header. While a reply runs the number is everything in the window right
// now (the conversation plus this reply's tool calls, results and excerpts); when it ends, what the
// conversation keeps. The bar's solid part is the conversation, the lighter part this reply's working
// context, and the tick is where auto-compaction fires. Click it for the breakdown.
export default function ContextMeter({ reading: taken, expect, fallbackWindow, compactPercent, live, speed, onInspect, actions }) {
  // A window read from another model or another context size is not the one the next reply gets.
  const reading = live ? taken : forDisplay(taken, expect);
  const [open, setOpen] = useState(false);
  const trigger = useRef(null), id = useId();
  const presence = usePresence(open);
  const close = (restore = true) => { setOpen(false); if (restore) trigger.current?.focus({ preventScroll: true }); };
  const panel = useFocusScope(open, () => close());
  const position = useAnchoredPopover(open, trigger, panel, close, 384);
  const view = meterView(reading, fallbackWindow, compactPercent);
  const shown = live ? view.total : view.used;
  const { limit } = compactionLimit(view.window, compactPercent);
  const label = `Context: ${shown.toLocaleString()}${view.window ? ` of ${view.window.toLocaleString()} tokens (${view.percent}%)` : ' tokens'}${view.warning ? `. ${view.warning}` : ''}. Show what is using it`;
  return <>
    <button ref={trigger} type="button" className="hc-ctx-pill" data-tone={view.tone} aria-label={label} title={view.warning || 'Context window: click for a breakdown'}
      aria-haspopup="dialog" aria-expanded={open} aria-controls={open ? id : undefined} onClick={() => setOpen(value => !value)}>
      {/* The bar sits under the number so the pill stays narrow: a wider one pushed the chat header onto two rows at 1280 px. */}
      <span className="hc-ctx-line">
        {live ? <span className="hc-ctx-live" aria-hidden="true"/> : view.warning ? <span aria-hidden="true" style={{ color: 'var(--ctx-c)' }}>⚠</span> : null}
        <span className="hc-ctx-num">{reading && !reading.measured ? '~' : ''}{formatTokens(shown)}</span>
        {view.window > 0 && <span className="hc-ctx-of">/ {formatTokens(view.window)}</span>}
        {live && speed > 0 && <span className="hc-ctx-speed">{speed} t/s</span>}
      </span>
      {view.window > 0 && <span className="hc-ctx-bar" aria-hidden="true">
        <span className="hc-ctx-track"><i style={{ width: `${view.usedShare * 100}%` }}/><i style={{ width: `${(live ? view.workingShare : 0) * 100}%` }}/></span>
        {limit > 0 && <span className="hc-ctx-tick" style={{ left: `${(limit / view.window) * 100}%` }}/>}
      </span>}
    </button>
    {presence.mounted && createPortal(<div ref={panel} id={id} role="dialog" aria-label="What is using the context window" tabIndex={-1}
      className="hc-popover hc-pop hc-ctx-pop" data-placement={position.up ? 'top' : 'bottom'} data-align="end" {...presence.props} style={position.style}>
      <ContextBreakdown reading={reading} fallbackWindow={fallbackWindow} compactPercent={compactPercent} live={live} actions={actions}
        onInspect={onInspect ? () => { close(false); onInspect(); } : null}/>
    </div>, document.body)}
  </>;
}
