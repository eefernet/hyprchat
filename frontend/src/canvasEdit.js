// Where a finished AI edit goes in the Canvas document.
//
// The edit was asked for with positions in the document as it was then, and the editor stays usable while
// the model works. Applying at those saved positions after the text moved overwrote unrelated text (found
// 2026-11-18). So the range is checked against the text it was meant to replace, followed if that text only
// moved, and refused otherwise.
export function locateEdit(doc, edit) {
  const text = String(doc ?? '');
  const original = String(edit?.original ?? '');
  const from = Number(edit?.from), to = Number(edit?.to);
  if (Number.isInteger(from) && Number.isInteger(to) && from >= 0 && to >= from && to <= text.length
      && text.slice(from, to) === original) return { from, to };
  if (!original) return null;                       // an empty target cannot be found again
  const first = text.indexOf(original);
  if (first === -1 || text.indexOf(original, first + 1) !== -1) return null;   // gone, or ambiguous
  return { from: first, to: first + original.length };
}
