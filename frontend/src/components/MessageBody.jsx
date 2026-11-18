import React from 'react';
import { MessageCards } from './ToolOutputCards.jsx';

// An assistant reply as its parts, in order: a stretch of text, then the cards of the tools that ran
// after it (see messageParts.js). A reply with no placed cards is a single part and renders exactly as
// it did before parts existed.
//
// Finished parts go through MemoMD, so they are not parsed again on every token; only the last part of
// a reply that is still streaming goes through mdStream.

// MemoMD compares `opts` by reference, and code blocks key their state on `opts.codePath`, so each part
// needs its own options object that stays the same from render to render.
const derived = new WeakMap();
const plain = [];
function partOptions(opts, index) {
  if (!index) return opts;
  let cache = plain;
  if (opts && typeof opts === 'object') {
    if (!derived.has(opts)) derived.set(opts, []);
    cache = derived.get(opts);
  }
  if (!cache[index]) cache[index] = { ...(opts || {}), codePath: `${opts?.codePath || ''}/part${index}` };
  return cache[index];
}

export default function MessageBody({ parts, streaming, md, mdStream, MemoMD, opts, display, messageText, t, onPreview, onOpenArtifact }) {
  const last = parts.length - 1;
  return parts.map((part, index) => {
    const raw = index ? part.text.replace(/^\s+/, '') : part.text;
    const text = display ? display(raw) : raw;
    const options = partOptions(opts, index);
    return <React.Fragment key={index}>
      {!text ? null : streaming && index === last ? mdStream(text, options) : <MemoMD content={text} md={md} opts={options}/>}
      <MessageCards cards={part.cards} slot={`p${index}`} t={t} onPreview={onPreview} onOpenArtifact={onOpenArtifact} messageText={messageText}/>
    </React.Fragment>;
  });
}
