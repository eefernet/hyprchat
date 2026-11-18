// Code fences opened with four or more backticks ("long fences"), which let a block contain ordinary
// ``` fences. A Markdown document with code blocks inside it has to be written that way; before this the
// renderer ended the block at the first inner fence and the rest of the document spilled out as chat text.
//
// The same happens, far more often, with a ```markdown block that holds code blocks: that is how models
// usually write a document, four-backtick rule or not. Both cases are handled here.
//
// The renderer splits a reply on ``` fences with a regular expression, so a long fence is rewritten into
// an ordinary one whose inner ``` are swapped for a marker (private-use characters that a reply does not
// contain). `unfoldFence` puts them back in the block's code, so what is shown, copied and previewed is
// exactly what was written. Pure, so node:test can import it.

export const FENCE_MARK = '';

const OPEN_LONG = /^([ \t]{0,3})(`{4,})([^`]*)$/;
const CLOSE_LONG = /^[ \t]{0,3}(`{4,})[ \t]*$/;
const TRIPLE = /^[ \t]{0,3}```(?!`)/;
const DOC_OPEN = /^[ \t]{0,3}```(?:markdown|md|mdx)(?=[\s:]|$)[^`]*$/i;   // a ```markdown block: it may hold a whole document
const INNER_OPEN = /^[ \t]{0,3}```[^\s`]/;                              // ```bash, ```python …: a fence that names a language
const BARE = /^[ \t]{0,3}```[ \t]*$/;
const MAY_NEST = /(^|\n)[ \t]{0,3}(````|```(?:markdown|md|mdx)(?=[\s:]|$))/i;

export function foldLongFences(text) {
  const source = String(text ?? '');
  if (!MAY_NEST.test(source)) return source;
  const out = [];
  let long = 0;          // backticks of the long fence we are inside, 0 when outside
  let triple = false;    // inside an ordinary fence: four backticks there are just text
  let doc = -1;          // inside a ```markdown block: how many inner fences are open (-1 = not inside one)
  const hide = line => line.split('```').join(FENCE_MARK);
  for (const line of source.split('\n')) {
    if (long) {
      const close = line.match(CLOSE_LONG);
      if (close && close[1].length >= long) { long = 0; out.push('```'); continue; }
      out.push(hide(line));
      continue;
    }
    // Models usually write a document as ```markdown with ordinary ``` code blocks inside it. A fence that
    // names a language can only OPEN a block, so inside a document it starts an inner block; a bare ```
    // closes the innermost one, and the document ends when none is open.
    if (doc >= 0) {
      if (INNER_OPEN.test(line)) { doc += 1; out.push(hide(line)); continue; }
      if (BARE.test(line)) {
        if (doc > 0) { doc -= 1; out.push(hide(line)); continue; }
        doc = -1; out.push(line); continue;
      }
      out.push(hide(line));
      continue;
    }
    if (!triple) {
      const open = line.match(OPEN_LONG);
      if (open) { long = open[2].length; out.push(`${open[1]}\`\`\`${open[3]}`); continue; }
      if (DOC_OPEN.test(line)) { doc = 0; out.push(line); continue; }
    }
    if (TRIPLE.test(line)) triple = !triple;
    out.push(line);
  }
  // Still open (the reply is being written): left open here; the renderer closes an unclosed fence itself.
  return out.join('\n');
}

export const unfoldFence = code => (String(code ?? '').includes(FENCE_MARK) ? String(code).split(FENCE_MARK).join('```') : code);
