// Fence labels and syntax rendering shared by streaming and saved messages.
const ALIASES = {
  js:'javascript', ts:'typescript', py:'python', sh:'bash', shell:'bash',
  zsh:'bash', yml:'yaml', html:'markup', xml:'markup', svg:'markup',
  'c++':'cpp', 'c#':'csharp', cs:'csharp', rb:'ruby', golang:'go',
  dockerfile:'docker', plaintext:'none', text:'none', txt:'none',
};

export function normalizeCodeLanguage(info='') {
  const label=String(info).trim().split(/\s+/)[0].toLowerCase();
  return Object.prototype.hasOwnProperty.call(ALIASES,label) ? ALIASES[label] : (/^[a-z0-9_+#-]+$/.test(label) ? label : '');
}

export function parseCodeFence(block) {
  const match=String(block).match(/^[ \t]{0,3}```([^\r\n`]*)\r?\n([\s\S]*?)\r?\n?[ \t]{0,3}```[ \t]*$/);
  if(!match)return null;
  return {lang:normalizeCodeLanguage(match[1]),code:match[2]};
}

export function highlightCode(code,lang,prism) {
  const language=normalizeCodeLanguage(lang);
  if(!prism?.languages || !Object.prototype.hasOwnProperty.call(prism.languages,language))return null;
  try{return prism.highlight(String(code),prism.languages[language],language);}
  catch{return null;}
}

// Separate light/dark palettes keep token contrast independent of accent color.
// CSS is scoped to code blocks; app backgrounds, fonts and inline code stay themed.
export function codeTokenStyle(background='') {
  const rgb=/^#([\da-f]{6})$/i.exec(background)?.[1];
  const light=rgb && (parseInt(rgb.slice(0,2),16)*.2126+parseInt(rgb.slice(2,4),16)*.7152+parseInt(rgb.slice(4,6),16)*.0722)>150;
  const colors=light
    ? {keyword:'#7b268f',string:'#216b35',number:'#97500e',function:'#005c9e',type:'#7b4e00',operator:'#00686c',comment:'#56616e',punctuation:'#38434e'}
    : {keyword:'#c792ea',string:'#a3d98d',number:'#f5bc7a',function:'#82caff',type:'#ffd580',operator:'#89ddff',comment:'#a0aab8',punctuation:'#c9d1d9'};
  return Object.fromEntries(Object.entries(colors).map(([key,value])=>[`--code-${key}`,value]));
}
