// Regression checks for the web interface. Run with:
//   python3 ~/.claude/skills/web-ui-check/uicheck.py test http://127.0.0.1:3333/ tests-ui.js
// Every check here exists because the behaviour it covers was once broken.
(async () => {
  const log=[], $=s=>document.querySelector(s);
  const ok=(n,c,d="")=>log.push((c?"PASS":"FAIL")+"  "+n+(d?"   ["+d+"]":""));
  const vis=s=>{const e=$(s);return !!e && !e.classList.contains("hide") && e.offsetParent!==null};
  const wait=ms=>new Promise(r=>setTimeout(r,ms));

  // ---- docs tab
  $("#tab-docs").click(); await wait(150);
  ok("docs tab opens", vis("#view-docs") && !vis("#view-ocr") && !vis("#view-kb"));
  ok("docs tab is marked selected", $("#tab-docs").getAttribute("aria-selected")==="true");
  ok("http example carries this server's address", ($("#d-base").textContent||"").startsWith("http"),
     $("#d-base").textContent);
  ok("docs note count is filled in", /^[1-9][0-9]*$/.test($("#d-notes").textContent), $("#d-notes").textContent);
  ok("docs has its own copy button", !!$("#cpdocs"));
  ok("docs does not overflow sideways", document.documentElement.scrollWidth<=window.innerWidth+2,
     document.documentElement.scrollWidth+" / "+window.innerWidth);

  ok("brand is just the name", $(".mark").textContent.trim()==="LLM Mini Toolkit",
     $(".mark").textContent.trim());

  // ---- sidebar
  ok("cli download link is gone", !document.querySelector('a[href="/minitoolkit.py"]'));
  ok("the gpu blurb is gone", !document.body.textContent.includes("One card"));
  ok("sidebar keeps the prompt button", !!$("#cpprompt"));
  $("#tab-ocr").click(); await wait(120);
  $("#godocs").click(); await wait(150);
  ok("'how it works' reaches the docs", vis("#view-docs"));

  // ---- an empty question used to do nothing at all
  $("#tab-kb").click(); $("#sub-ask").click(); await wait(150);
  $("#q").value=""; $("#ask").click(); await wait(200);
  ok("empty question says something", $("#askmsg").textContent.length>0, $("#askmsg").textContent);
  ok("empty question sends nothing", $("#ask").disabled===false && !vis("#result"));

  // ---- the three levels replaced the old summary/direct toggle
  ok("fetch sits leftmost, being the one to prefer",
     [...document.querySelectorAll(".sub button")].filter(b=>b.id.startsWith("m-"))
       .map(b=>b.textContent).join(",")==="fetch,search,ask",
     [...document.querySelectorAll(".sub button")].filter(b=>b.id.startsWith("m-"))
       .map(b=>b.textContent).join(","));
  ok("three levels are offered", !!$("#m-search") && !!$("#m-fetch") && !!$("#m-ask")
     && !document.querySelector("#m-sum,#m-dir"));
  ok("fetch is the one selected by default",
     $("#m-fetch").getAttribute("aria-selected")==="true" && MODE==="fetch", MODE);
  $("#m-search").click(); await wait(80);
  ok("picking search renames the button", $("#ask").textContent==="search", $("#ask").textContent);
  ok("search says no model is used", /no model/.test($("#modehint").textContent), $("#modehint").textContent);
  $("#m-ask").click(); await wait(80);
  ok("picking ask renames the button", $("#ask").textContent==="ask" && MODE==="ask");
  ok("ask states its cost", /20-90/.test($("#modehint").textContent), $("#modehint").textContent);

  // ---- search really runs, with no model behind it
  const loadedBefore=$("#b-model").textContent;
  $("#m-search").click(); $("#q").value="stm32 swd debug"; await wait(80);
  const t0=Date.now(); $("#ask").click();
  for(let i=0;i<60 && $("#ask").disabled;i++) await wait(100);
  const took=(Date.now()-t0)/1000;
  const hits=document.querySelectorAll("#hits .hit").length;
  ok("search returns hits", hits>0, hits+" hits in "+took.toFixed(2)+"s");
  ok("search is fast because nothing loads", took<3, took.toFixed(2)+"s");
  ok("search reports that no model was used", /no model used/.test($("#askmsg").textContent),
     $("#askmsg").textContent);
  ok("titles are not double-escaped",
     !/&(amp|lt|gt|quot|nbsp);/i.test(document.querySelector("#hits .ht")?.textContent||"")
     && !/&(amp|lt|gt|quot|nbsp);/i.test($("#hits").textContent),
     document.querySelector("#hits .ht")?.textContent);
  ok("a hit shows id and score", /#\d+ · \d/.test(document.querySelector("#hits .hs")?.textContent||""),
     document.querySelector("#hits .hs")?.textContent);
  ok("search loaded no model", $("#b-model").textContent===loadedBefore,
     loadedBefore+" -> "+$("#b-model").textContent);

  // ---- switching level clears what the previous one left behind
  $("#m-fetch").click(); await wait(120);
  ok("changing level clears the old result",
     !vis("#hits") && $("#answer").textContent==="" && !vis("#result"));

  // ---- the level buttons stayed live mid-run and a result landed under the wrong one
  setAsking(true); await wait(80);
  const before=MODE;
  ok("level buttons lock while running",
     $("#m-search").disabled && $("#m-fetch").disabled && $("#m-ask").disabled);
  $("#m-ask").click(); setMode("ask"); await wait(80);
  ok("level does not change while running", MODE===before, "mode="+MODE);
  ok("the run button locks too", $("#ask").disabled===true);
  setAsking(false); setMode("fetch"); await wait(80);
  ok("levels work again once done", MODE==="fetch" && !$("#m-ask").disabled);

  // ---- the library browses catalog -> deck -> guide, the way the site does
  $("#sub-list").click(); await wait(900);
  ok("notes opens on the deck catalog", vis("#lib-catalog") && !vis("#lib-deck") && !vis("#lib-note"));
  const decks=document.querySelectorAll("#decks .card").length;
  ok("every deck is listed", decks>20, decks+" decks · "+$("#dcount").textContent);
  ok("own notes get a deck of their own",
     [...document.querySelectorAll("#decks .ct")].some(e=>e.textContent==="own notes"));
  $("#dfilter").value="stm32"; renderDecks(); await wait(150);
  ok("the catalog filters", document.querySelectorAll("#decks .card").length<decks,
     $("#dcount").textContent);
  $("#dfilter").value=""; renderDecks(); await wait(100);

  await openDeck("stm32-debug"); await wait(500);
  ok("card previews carry no raw markdown",
     ![...document.querySelectorAll("#notes .cd")].some(e=>/[`*#]/.test(e.textContent)),
     document.querySelector("#notes .cd")?.textContent.slice(0,50));
  ok("a deck opens its guides", vis("#lib-deck") && document.querySelectorAll("#notes .card").length===8,
     document.querySelectorAll("#notes .card").length+" guides");
  ok("guides are numbered in deck order",
     document.querySelector("#notes .cnum")?.textContent==="01",
     document.querySelector("#notes .cnum")?.textContent);
  ok("the breadcrumb shows where you are", /library.*stm32-debug/s.test($("#crumb").textContent),
     $("#crumb").textContent.replace(/\s+/g," "));
  $("#filter").value="rtt"; renderNotes(); await wait(150);
  const narrowed=document.querySelectorAll("#notes .card").length;
  ok("a deck can be searched", narrowed>0 && narrowed<8, $("#gcount").textContent);
  $("#filter").value=""; renderNotes(); await wait(100);

  await openNote(261); await wait(600);
  ok("the way back stays on screen", $("#crumb").getBoundingClientRect().top>=0
     && $("#crumb").getBoundingClientRect().top<window.innerHeight,
     Math.round($("#crumb").getBoundingClientRect().top)+"px");
  ok("a guide opens rendered", vis("#lib-note") && $("#gtitle").textContent.includes("SWD"),
     $("#gtitle").textContent);
  ok("markdown became real headings", $("#gbody").querySelectorAll(".md-h1,.md-h2").length>5,
     $("#gbody").querySelectorAll(".md-h1,.md-h2").length+" headings");
  // the published page for this guide has 13 code blocks, 7 tables and 5 callouts
  ok("code blocks survive the conversion",
     $("#gbody").querySelectorAll("pre.md-code").length>=13,
     $("#gbody").querySelectorAll("pre.md-code").length+" of 13 on the site");
  ok("tables survive the conversion",
     $("#gbody").querySelectorAll("table.md-table").length===7,
     $("#gbody").querySelectorAll("table.md-table").length+" of 7 on the site");
  ok("callouts survive as blockquotes",
     $("#gbody").querySelectorAll("blockquote.md-quote").length===5,
     $("#gbody").querySelectorAll("blockquote.md-quote").length+" of 5 on the site");
  ok("tables have real header cells", $("#gbody").querySelector("table.md-table th")?.textContent==="Signal",
     $("#gbody").querySelector("table.md-table th")?.textContent);
  {  // ascii diagrams inside code legitimately look like markdown, so only prose counts
    const clone=$("#gbody").cloneNode(true);
    clone.querySelectorAll("pre").forEach(e=>e.remove());
    ok("no raw markdown is left on screen",
       !/^#{1,4}\s|\|\s*---|```/m.test(clone.textContent));
  }
  ok("the guide links back to its source",
     /href="https?:\/\//.test($("#gmeta").innerHTML));
  ok("the breadcrumb reaches the guide", /stm32-debug/.test($("#crumb").textContent));
  $("#crumb").querySelector('a[data-go="deck"]').click(); await wait(500);
  ok("the breadcrumb walks back to the deck", vis("#lib-deck") && !vis("#lib-note"));
  $("#crumb").querySelector('a[data-go="catalog"]').click(); await wait(500);
  ok("and back to the catalog", vis("#lib-catalog"));

  await openNote(99999); await wait(400);
  ok("a missing guide says so", /no longer/.test($("#libmsg").textContent)
     && vis("#libmsg") && vis("#lib-catalog"), $("#libmsg").textContent);
  ok("the stats line was not used for the message", /deck/.test($("#dcount").textContent),
     $("#dcount").textContent);
  await openNote(261); await wait(600);
  ok("the message clears on the next guide", !vis("#libmsg"));

  // ---- a note's url lands in an href
  ok("non-http urls are dropped",
     safeUrl("javascript:alert(1)")==="" && safeUrl("ftp://x")==="" && safeUrl("https://x.dev/a")==="https://x.dev/a");
  ok("quotes are escaped too", esc('a"b<c')==='a&quot;b&lt;c', esc('a"b<c'));

  // ---- .hide lost to .stage on source order, so the progress line never went away
  $("#sub-ask").click(); await wait(150);
  $("#result").classList.remove("hide");
  $("#stage").classList.remove("hide"); await wait(80);
  const shown=vis("#stage"); $("#stage").classList.add("hide"); await wait(60);
  ok(".hide really hides the progress line", shown && !vis("#stage"));

  return log;
})()
