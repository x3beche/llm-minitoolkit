(async () => {
  const log=[], $=s=>document.querySelector(s);
  const ok=(n,c,d="")=>log.push((c?"PASS":"FAIL")+"  "+n+(d?"   ["+d+"]":""));
  const vis=s=>{const e=$(s);return !!e&&!e.classList.contains("hide")&&e.offsetParent!==null};
  const wait=ms=>new Promise(r=>setTimeout(r,ms));
  const run=async(mode,q,maxS)=>{ setMode(mode); $("#q").value=q; $("#ask").click();
    for(let i=0;i<maxS*10 && $("#ask").disabled;i++) await wait(100); };
  $("#tab-kb").click(); await wait(300);

  await run("search","trustzone secure world",40);
  ok("search still shows cards", document.querySelectorAll("#hits .hit").length>0,
     document.querySelectorAll("#hits .hit").length+" cards");
  ok("search cards carry a heading", /matching note/.test($(".hh")?.textContent||""), $(".hh")?.textContent);
  ok("search offers a copy button", vis("#outrow") && vis("#copyout"));
  ok("search has no raw-text toggle", !vis("#toggleraw"));
  ok("search copy payload is built", COPYTEXT.length>100 && /\[#\d+\]/.test(COPYTEXT),
     COPYTEXT.length+" chars");

  await run("fetch","trustzone",240);
  const fc=document.querySelectorAll("#hits .hit").length;
  ok("fetch shows the articles as cards", fc>0 && vis("#hits"), fc+" cards");
  ok("fetch cards say what was opened", /article\(s\) opened in full/.test($(".hh")?.textContent||""),
     $(".hh")?.textContent);
  ok("fetch hides the wall of text by default", !vis("#answer"));
  ok("fetch offers the raw text on request", vis("#toggleraw"));
  $("#toggleraw").click(); await wait(200);
  ok("the raw text opens", vis("#answer") && $("#answer").textContent.length>1000,
     nf($("#answer").textContent.length)+" chars");
  ok("the toggle renames itself", /hide the raw/.test($("#toggleraw").textContent));
  ok("fetch copy payload is the articles", COPYTEXT.length>1000 && /=====/.test(COPYTEXT),
     nf(COPYTEXT.length)+" chars");
  const card=document.querySelector("#hits .hit");
  const id=+(card.querySelector(".hs").textContent.match(/#(\d+)/)||[])[1];
  card.click(); await wait(700);
  ok("clicking an article opens it", vis("#lib-note") && NOTE?.id===id, "#"+NOTE?.id);
  $("#sub-ask").click(); await wait(200);

  await run("ask","Which STM32 pins carry SWDIO and SWCLK?",300);
  ok("ask shows its sources as cards", document.querySelectorAll("#hits .hit").length>0,
     document.querySelectorAll("#hits .hit").length+" cards");
  ok("ask cards say what it read", /answered from/.test($(".hh")?.textContent||""), $(".hh")?.textContent);
  ok("ask still shows the answer", vis("#answer") && $("#answer").textContent.trim().length>0,
     $("#answer").textContent.trim().slice(0,50));
  ok("ask copy payload has answer and sources",
     /\[#\d+\]/.test(COPYTEXT) && /Sources:/.test(COPYTEXT), COPYTEXT.length+" chars");
  ok("ask has no raw-text toggle", !vis("#toggleraw"));
  {  // notes grew past 20k chars each and four of them overflowed the window,
     // which made llama-server answer 400 to every question
    const cells=[...document.querySelectorAll("#metrics .m")].map(e=>
      e.querySelector(".mk").textContent+"="+e.querySelector(".mv").textContent.trim());
    const ctx=(cells.find(c=>/^context=/.test(c))||"").match(/(\d+)%/);
    ok("the prompt stays inside the window", ctx && +ctx[1]<100, ctx?ctx[0]:"no context cell");
    ok("it still opened several notes",
       document.querySelectorAll("#hits .hit").length>=1,
       document.querySelectorAll("#hits .hit").length+" notes");
  }

  setMode("search"); await wait(200);
  ok("changing level clears it all", !vis("#hits") && !vis("#outrow") && COPYTEXT==="");
  return log;
})()
