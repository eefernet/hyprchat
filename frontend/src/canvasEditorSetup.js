// CodeMirror 6 factory for the Artifact Canvas editor.
//
// This module is loaded via dynamic import() from ArtifactCanvas.jsx, so all
// @codemirror/* packages land in their own lazy chunk — the main bundle pays
// nothing until the user actually opens the canvas.

import { basicSetup } from 'codemirror';
import { EditorView, keymap } from '@codemirror/view';
import { Compartment, EditorState } from '@codemirror/state';
import { MergeView } from '@codemirror/merge';
import { indentWithTab } from '@codemirror/commands';
import { javascript } from '@codemirror/lang-javascript';
import { python } from '@codemirror/lang-python';
import { markdown } from '@codemirror/lang-markdown';
import { html } from '@codemirror/lang-html';
import { css } from '@codemirror/lang-css';
import { json } from '@codemirror/lang-json';
import { oneDark } from '@codemirror/theme-one-dark';

function langFor(filename){
  const ext=String(filename||"").toLowerCase().split(".").pop();
  switch(ext){
    case "js": case "jsx": case "mjs": case "cjs": return javascript({jsx:true});
    case "ts": case "tsx": return javascript({jsx:true,typescript:true});
    case "py": case "pyw": return python();
    case "md": case "markdown": case "mdx": return markdown();
    case "html": case "htm": case "xml": case "svg": return html();
    case "css": case "scss": case "less": return css();
    case "json": case "jsonl": case "ndjson": return json();
    default: return [];
  }
}

// Perceived luminance of a #rrggbb theme background — decides dark vs light chrome.
export function isDarkBg(hex){
  const m=/^#?([0-9a-f]{6})$/i.exec(String(hex||"").trim());
  if(!m)return true;
  const v=parseInt(m[1],16);
  const r=(v>>16)&255,g=(v>>8)&255,b=v&255;
  return (0.2126*r+0.7152*g+0.0722*b)<140;
}

export function createEditor({parent,doc,filename,t,onDocChanged}){
  const dark=isDarkBg(t.bg);
  const chrome=EditorView.theme({
    "&":{backgroundColor:"transparent",color:t.text,height:"100%",fontSize:"12.5px"},
    ".cm-scroller":{fontFamily:"inherit",lineHeight:1.6,overflow:"auto"},
    ".cm-content":{caretColor:t.acc},
    ".cm-gutters":{backgroundColor:"transparent",color:t.mut,border:"none"},
    "&.cm-focused":{outline:"none"},
    ".cm-activeLine":{backgroundColor:dark?"rgba(255,255,255,.035)":"rgba(0,0,0,.045)"},
    ".cm-activeLineGutter":{backgroundColor:"transparent"},
    ".cm-selectionBackground, &.cm-focused .cm-selectionBackground":{backgroundColor:`${t.acc}40`},
    ".cm-cursor":{borderLeftColor:t.acc},
    ".cm-panels":{backgroundColor:t.surface,color:t.text},
    ".cm-searchMatch":{backgroundColor:`${t.warm}44`},
  },{dark});
  const langComp=new Compartment();
  const extensions=[
    // oneDark first so the chrome theme (later = higher precedence) can make
    // the background transparent while keeping oneDark's syntax colors.
    ...(dark?[oneDark]:[]),
    basicSetup,
    keymap.of([indentWithTab]),
    langComp.of(langFor(filename)),
    chrome,
    EditorView.lineWrapping,
    EditorView.updateListener.of(u=>{if(u.docChanged&&onDocChanged)onDocChanged();}),
  ];
  const view=new EditorView({doc:doc??"",extensions,parent});
  return {
    view,
    getDoc:()=>view.state.doc.toString(),
    getSelection:()=>{
      const sel=view.state.selection.main;
      return {from:sel.from,to:sel.to,text:view.state.sliceDoc(sel.from,sel.to)};
    },
    replaceRange:(from,to,insert)=>{
      view.dispatch({changes:{from,to,insert},selection:{anchor:from,head:from+insert.length},scrollIntoView:true});
    },
    destroy:()=>view.destroy(),
  };
}

// Read-only side-by-side diff (AI-edit preview in the Artifact Canvas).
// Word-level change highlighting and collapsed unchanged regions come from
// @codemirror/merge; both panes are non-editable.
export function createDiffView({parent,original,modified,t}){
  const dark=isDarkBg(t.bg);
  const chrome=EditorView.theme({
    "&":{backgroundColor:"transparent",color:t.text,fontSize:"11px"},
    ".cm-scroller":{fontFamily:"inherit",lineHeight:1.55,overflow:"auto"},
    ".cm-gutters":{backgroundColor:"transparent",color:t.mut,border:"none"},
    "&.cm-focused":{outline:"none"},
    ".cm-changedLine":{backgroundColor:`${t.ok}14`},
    ".cm-changedText":{backgroundColor:`${t.ok}33`},
    ".cm-deletedChunk":{backgroundColor:`${t.err}14`},
    ".cm-deletedText":{backgroundColor:`${t.err}33`},
    ".cm-collapsedLines":{color:t.mut,backgroundColor:dark?"rgba(255,255,255,.04)":"rgba(0,0,0,.05)"},
  },{dark});
  const side=[
    ...(dark?[oneDark]:[]),
    chrome,
    EditorView.lineWrapping,
    EditorView.editable.of(false),
    EditorState.readOnly.of(true),
  ];
  const mv=new MergeView({
    a:{doc:original??"",extensions:side},
    b:{doc:modified??"",extensions:side},
    parent,
    highlightChanges:true,
    gutter:true,
    collapseUnchanged:{margin:3,minSize:4},
  });
  return {destroy:()=>mv.destroy()};
}
