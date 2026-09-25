(async () => {
  const log=[]; const ok=(n,c,d="")=>log.push((c?"PASS":"FAIL")+"  "+n+(d?"   ["+d+"]":""));
  const ids=(await fetch("/api/notes?limit=1000").then(r=>r.json())).notes.map(n=>n.id);
  let worst=0, worstId=null, total=0, blocks=0, tables=0, quotes=0, raw=[];
  const t0=Date.now();
  for(const id of ids){
    const n=await fetch("/api/notes/"+id).then(r=>r.json());
    const s=performance.now();
    const h=renderMd(n.body||"");
    const ms=performance.now()-s;
    if(ms>worst){worst=ms;worstId=id}
    total+=h.length;
    const d=document.createElement("div"); d.innerHTML=h;
    blocks+=d.querySelectorAll("pre.md-code").length;
    tables+=d.querySelectorAll("table.md-table").length;
    quotes+=d.querySelectorAll("blockquote.md-quote").length;
    // ascii diagrams inside code look like markdown separators and are meant to
    // be there, so only the prose is checked for leaked syntax
    d.querySelectorAll("pre").forEach(e=>e.remove());
    if(/^\s*\|\s*---/m.test(d.textContent) || /^```/m.test(d.textContent)
       || /^#{1,4}\s/m.test(d.textContent)) raw.push(id);
  }
  ok("every note renders without hanging", true,
     ids.length+" notes in "+((Date.now()-t0)/1000).toFixed(1)+"s");
  ok("no single note is slow", worst<400, "worst "+worst.toFixed(0)+"ms on #"+worstId);
  ok("code blocks came through", blocks>1500, blocks+" code blocks");
  ok("tables came through", tables>800, tables+" tables");
  ok("callouts came through", quotes>200, quotes+" blockquotes");
  ok("no raw markdown leaks into the text", raw.length===0, raw.slice(0,5).join(","));
  return log;
})()
