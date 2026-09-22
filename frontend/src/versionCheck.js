// A tab opened before a deploy keeps running the old bundle (that hid the new Daedalus
// card once). Compare the entry script this page loaded with the one index.html names now.
export function entryScript(html){
  const match=String(html||'').match(/assets\/index-[\w-]+\.js/);
  return match?match[0]:'';
}
export function loadedEntry(doc=typeof document!=='undefined'?document:null){
  if(!doc)return '';
  for(const script of doc.querySelectorAll('script[src]')){const name=entryScript(script.getAttribute('src'));if(name)return name;}
  return '';
}
export function isStale(loaded,current){return !!loaded&&!!current&&loaded!==current;}
export async function checkForNewVersion(fetcher=fetch){
  try{
    const response=await fetcher('/index.html',{cache:'no-store'});
    if(!response.ok)return false;
    return isStale(loadedEntry(),entryScript(await response.text()));
  }catch{return false;}
}
