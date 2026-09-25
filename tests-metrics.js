(async () => {
  const log=[], $=s=>document.querySelector(s);
  const ok=(n,c,d="")=>log.push((c?"PASS":"FAIL")+"  "+n+(d?"   ["+d+"]":""));
  const vis=s=>{const e=$(s);return !!e&&!e.classList.contains("hide")&&e.offsetParent!==null};
  const wait=ms=>new Promise(r=>setTimeout(r,ms));
  const run=async(mode,q,maxS)=>{ setMode(mode); $("#q").value=q; $("#ask").click();
    for(let i=0;i<maxS*10 && $("#ask").disabled;i++) await wait(100);
    return [...document.querySelectorAll("#metrics .m")].map(e=>
      e.querySelector(".mk").textContent+"="+e.querySelector(".mv").textContent.trim()); };
  $("#tab-kb").click(); await wait(300);

  const s=await run("search","trustzone secure world",30);
  ok("search shows metrics", vis("#metrics") && s.length>=4, s.join(" | "));
  ok("search reports the model was untouched", s.some(x=>/model=not used/.test(x)),
     s.find(x=>/^model=/.test(x)));

  const f=await run("fetch","i want to know things about trustzone",180);
  ok("fetch shows metrics", vis("#metrics") && f.length>=5, f.join(" | "));
  ok("fetch reports returned tokens", f.some(x=>/^returned=/.test(x)), f.find(x=>/^returned=/.test(x)));
  ok("no cell is left without a label",
     ![...document.querySelectorAll("#metrics .m")].some(e=>!e.querySelector(".mk").textContent.trim()));
  {  // a short last row used to leave dead space at the end of the strip
    const cells=[...document.querySelectorAll("#metrics .m")];
    const strip=document.querySelector("#metrics .mx").getBoundingClientRect();
    const rows={};
    cells.forEach(e=>{const r=e.getBoundingClientRect();
      (rows[Math.round(r.top)] ||= []).push(r)});
    const widths=Object.values(rows).map(rs=>Math.round(rs.reduce((a,r)=>a+r.width,0)));
    ok("every row fills the strip", widths.every(w=>Math.abs(w-strip.width)<=4),
       widths.join(" / ")+" of "+Math.round(strip.width)+"px, "+widths.length+" row(s)");
    const unit=strip.width/4;                 // four columns at this width
    const spans=cells.map(e=>e.getBoundingClientRect().width/unit);
    ok("every cell is a whole number of columns",
       spans.every(x=>Math.abs(x-Math.round(x))<0.06 && Math.round(x)>=1),
       spans.map(x=>x.toFixed(2)).join(" "));
    const lefts=new Set(cells.map(e=>Math.round(e.getBoundingClientRect().left)));
    ok("cells start on column boundaries", lefts.size<=4, [...lefts].sort((a,b)=>a-b).join(","));
    ok("nothing overflows its cell", !cells.some(e=>e.scrollWidth>e.clientWidth+1));
  }
  ok("fetch reports the picking cost", f.some(x=>/picking cost=/.test(x)),
     f.find(x=>/picking cost=/.test(x)));

  const a=await run("ask","Which STM32 pins carry SWDIO and SWCLK?",240);
  ok("ask shows metrics", vis("#metrics") && a.length>=6, a.join(" | "));
  ok("ask reports tokens written", a.some(x=>/^written=\d/.test(x)), a.find(x=>/^written=/.test(x)));
  ok("ask reports generation speed", a.some(x=>/^speed=[\d.]+/.test(x)), a.find(x=>/^speed=/.test(x)));
  ok("ask reports context use", a.some(x=>/^context=\d+%/.test(x)), a.find(x=>/^context=/.test(x)));
  ok("ask reports time to first token", a.some(x=>/first token=/.test(x)),
     a.find(x=>/first token=/.test(x)));

  setMode("search"); await wait(200);
  ok("changing level clears the metrics", !vis("#metrics"));
  return log;
})()
