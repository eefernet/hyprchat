// Width rules for the right-hand pane (SidePane.jsx). Pure; the hook is usePaneResize.js.
export const MIN_WIDTH=260;       // narrower than this the header controls no longer fit
export const WIDE_ENOUGH=900;     // always allowed, as before
export const CHAT_ROOM=420;       // what the chat keeps when the pane is dragged wider than that
export const KEY_STEP=24;
export const DEFAULT_WIDTH=420;

// `room` = the pane's width plus the width of the column beside it (the chat), in the app's own units.
export function maxWidth(room){
  const space=Number(room);
  if(!Number.isFinite(space)||space<=0)return WIDE_ENOUGH;
  return Math.max(WIDE_ENOUGH,Math.floor(space-CHAT_ROOM));
}

export function clampWidth(width,room){
  const value=Number(width);
  if(!Number.isFinite(value))return DEFAULT_WIDTH;
  return Math.max(MIN_WIDTH,Math.min(maxWidth(room),Math.round(value)));
}

// The handle is the pane's LEFT edge: moving the pointer left makes the pane wider. Pointer positions
// are in screen pixels while the pane is laid out inside the app's zoom (the UI scale), so the
// distance is divided by it; otherwise the edge runs ahead of or behind the cursor.
export function draggedWidth(startWidth,startX,x,scale,room){
  const zoom=Number(scale)>0?Number(scale):1;
  return clampWidth(startWidth+(startX-x)/zoom,room);
}

export function keyWidth(width,key,room){
  if(key==='ArrowLeft')return clampWidth(width+KEY_STEP,room);
  if(key==='ArrowRight')return clampWidth(width-KEY_STEP,room);
  if(key==='Home')return clampWidth(maxWidth(room),room);
  if(key==='End')return MIN_WIDTH;
  return null;
}

export function storedWidth(raw){
  const value=parseInt(raw,10);
  return Number.isFinite(value)&&value>=MIN_WIDTH?value:DEFAULT_WIDTH;
}
