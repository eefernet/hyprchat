// The Assistant panel's editable copy of the profile.
//
// The panel re-reads the server when the window regains focus. That used to replace the form with the
// saved values, so text typed and not yet saved was lost by switching to another window and back
// (found 2026-11-18). A draft the user has changed is kept; an untouched one follows the server.
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// `current`: what the form holds. `loaded`: the server copy it was last filled from. `incoming`: the fresh
// server copy. Returns what the form should hold now.
export function mergeDraft(current, loaded, incoming) {
  if (!current || !loaded || same(current, loaded)) return incoming;
  return current;
}
