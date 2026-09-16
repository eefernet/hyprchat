// Deterministic identity colors — stable across reloads/reorders because they
// hash a stable id (caldav account id, email sender address), never a list index.
// Shared by CalendarPanel (per-calendar event colors) and EmailPanel (sender avatars).

export function hashIdx(str,n){
  let h=5381;
  const s=String(str||"");
  for(let i=0;i<s.length;i++)h=((h<<5)+h+s.charCodeAt(i))>>>0;
  return h%n;
}

export function identityPalette(t){
  return [t.warm,t.pink,t.ok,t.f1,t.f4,t.acc2];
}

export function colorFor(id,t){
  const p=identityPalette(t);
  return p[hashIdx(id,p.length)];
}
