// Third-party libraries, bundled locally (no runtime CDN).
//
// The component code in main.jsx accesses these as window.* globals — exactly
// as it did when they loaded from CDN <script> tags. We replicate that surface
// here so the ~9.8k lines of component code stay unchanged.
//
//   Eager (needed during render, can't be awaited):
//     React / ReactDOM, Prism (+ common language grammars), KaTeX (+ auto-render).
//   Lazy (heavy, only needed when a diagram/chart/PDF export actually happens):
//     mermaid (+ svg-pan-zoom), chart.js, html2pdf.js — loaded on demand via
//     the ensureX() helpers, which the call sites in main.jsx trigger.

import React from 'react';
import * as ReactDOMFull from 'react-dom';
import { createRoot, hydrateRoot } from 'react-dom/client';

// ── Prism (eager) ──
// prism-setup must come before the component grammars (see its comment).
import Prism from './prism-setup.js';
import 'prismjs/themes/prism-tomorrow.css';
import 'prismjs/components/prism-markup';      // html / xml / svg
import 'prismjs/components/prism-css';
import 'prismjs/components/prism-clike';
import 'prismjs/components/prism-javascript';
import 'prismjs/components/prism-jsx';
import 'prismjs/components/prism-typescript';
import 'prismjs/components/prism-tsx';
import 'prismjs/components/prism-json';
import 'prismjs/components/prism-python';
import 'prismjs/components/prism-bash';
import 'prismjs/components/prism-yaml';
import 'prismjs/components/prism-markdown';
import 'prismjs/components/prism-sql';
import 'prismjs/components/prism-go';
import 'prismjs/components/prism-rust';
import 'prismjs/components/prism-c';
import 'prismjs/components/prism-cpp';
import 'prismjs/components/prism-java';
import 'prismjs/components/prism-csharp';
import 'prismjs/components/prism-ruby';
import 'prismjs/components/prism-php';
import 'prismjs/components/prism-toml';
import 'prismjs/components/prism-docker';
import 'prismjs/components/prism-diff';
import 'prismjs/components/prism-ini';

// ── KaTeX (eager) ──
import katex from 'katex';
import 'katex/dist/katex.min.css';
import renderMathInElement from 'katex/contrib/auto-render';

// Expose the eager globals (matches the former CDN globals exactly).
if (typeof window !== 'undefined') {
  const ReactDOM = { ...ReactDOMFull, createRoot, hydrateRoot };
  window.React = React;
  window.ReactDOM = ReactDOM;
  window.Prism = Prism;
  window.katex = katex;
  window.renderMathInElement = renderMathInElement;
}

// ── Lazy heavy libs ──
// Each ensureX() loads its chunk once, assigns the window global the component
// code expects, and resolves. Call sites trigger these and re-render when the
// global appears.
let _mermaidP, _chartP, _html2pdfP;

export function ensureMermaid() {
  if (!_mermaidP) {
    // svg-pan-zoom is only used for the fullscreen mermaid viewer, so it rides
    // along with the mermaid chunk.
    _mermaidP = Promise.all([
      import('mermaid'),
      import('svg-pan-zoom'),
    ]).then(([m, s]) => {
      window.mermaid = m.default || m;
      window.svgPanZoom = s.default || s;
      return window.mermaid;
    });
  }
  return _mermaidP;
}

export function ensureChart() {
  if (!_chartP) {
    // chart.js/auto auto-registers all controllers/elements/scales, matching
    // the old UMD chart.umd.min.js bundle.
    _chartP = import('chart.js/auto').then((m) => {
      window.Chart = m.default || m;
      return window.Chart;
    });
  }
  return _chartP;
}

export function ensureHtml2pdf() {
  if (!_html2pdfP) {
    _html2pdfP = import('html2pdf.js').then((m) => {
      window.html2pdf = m.default || m;
      return window.html2pdf;
    });
  }
  return _html2pdfP;
}

// Mirror onto window so the (non-module-scoped) component body can call them.
if (typeof window !== 'undefined') {
  window.ensureMermaid = ensureMermaid;
  window.ensureChart = ensureChart;
  window.ensureHtml2pdf = ensureHtml2pdf;
}
