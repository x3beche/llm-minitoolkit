(async () => {
  const log=[], $=s=>document.querySelector(s);
  const ok=(n,c,d="")=>log.push((c?"PASS":"FAIL")+"  "+n+(d?"   ["+d+"]":""));
  const vis=s=>{const e=$(s);return !!e&&!e.classList.contains("hide")&&e.offsetParent!==null};
  const wait=ms=>new Promise(r=>setTimeout(r,ms));

  // ---- OCR -> kutuphane (sayfa durumunu taklit et, model calistirmadan)
  $("#tab-ocr").click(); await wait(200);
  PAGES=[{name:"MT6365_p17.png",state:"ok",text:"# Charger block\n\nsome read text\n\n| a | b |\n| --- | --- |\n| 1 | 2 |",
          thumb:"",wall:12,open:true},
         {name:"MT6365_p18.png",state:"ok",text:"second page text",thumb:"",wall:11,open:false}];
  renderPages(); await wait(200);
  ok("a read page offers to save it", !!document.querySelector("[data-k]"));
  ok("several pages offer one document", vis("#tolib"));
  document.querySelector("[data-k]").click(); await wait(300);
  ok("saving a page opens the add form", vis("#kb-add") && !vis("#view-ocr"));
  ok("the title is guessed from the text", $("#n-title").value==="Charger block", $("#n-title").value);
  ok("tags are seeded from the file", /ocr/.test($("#n-tags").value), $("#n-tags").value);
  ok("the body is the read text", /some read text/.test($("#n-body").value));
  ok("it says where it came from", /MT6365_p17/.test($("#savemsg").textContent), $("#savemsg").textContent);

  $("#tab-ocr").click(); await wait(200);
  $("#tolib").click(); await wait(300);
  ok("all pages become one document", /MT6365_p17/.test($("#n-body").value)
     && /second page text/.test($("#n-body").value));
  ok("each page is marked in it", ($("#n-body").value.match(/^## /gm)||[]).length===2,
     ($("#n-body").value.match(/^## /gm)||[]).length+" markers");
  ok("it says how many pages", /2 page/.test($("#savemsg").textContent), $("#savemsg").textContent);
  PAGES=[]; renderPages();

  // ---- toplu silme
  $("#tab-kb").click(); $("#sub-list").click(); await wait(900);
  // silmek icin once birkac test notu koy
  const made=[];
  for(const t of ["zz-delete-me-1","zz-delete-me-2","zz-keep-me"]){
    const r=await fetch("/api/notes",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({title:t,tags:"zztest",body:"throwaway"})}).then(x=>x.json());
    made.push(r.id);
  }
  await openDeck("own"); await wait(600);
  const before=NOTES.length;
  ok("the test notes are there", before>=3, before+" notes");
  ok("selecting is off by default", !SELECTING && !vis("#delsel"));
  $("#selmode").click(); await wait(200);
  ok("select mode turns on", SELECTING && /cancel/.test($("#selmode").textContent));
  ok("cards show a checkbox", getComputedStyle(document.querySelector(".card .cpick")).display!=="none");
  const cards=[...document.querySelectorAll("#notes .card")];
  const pick=cards.filter(c=>/zz-delete-me/.test(c.querySelector(".ct").textContent));
  pick.forEach(c=>c.click());
  await wait(200);
  ok("clicking selects instead of opening", PICKED.size===2 && vis("#lib-deck"), PICKED.size+" picked");
  ok("the count reflects it", /2 selected/.test($("#gcount").textContent), $("#gcount").textContent);
  ok("the delete button appears", vis("#delsel") && /delete 2/.test($("#delsel").textContent),
     $("#delsel").textContent);
  $("#delsel").click(); await wait(200);
  ok("one click only arms it", NOTES.length===before, /click again/.test($("#delsel").textContent)?"armed":"NOT armed");
  $("#delsel").click();
  for(let i=0;i<80 && NOTES.length===before;i++) await wait(100);
  ok("the second click deletes", NOTES.length===before-2, before+" -> "+NOTES.length);
  ok("the kept note survives", NOTES.some(n=>n.title==="zz-keep-me"));
  ok("it says what happened", /2 note\(s\) deleted/.test($("#libmsg").textContent), $("#libmsg").textContent);
  ok("select mode resets", !SELECTING && PICKED.size===0);
  // temizle
  const keep=NOTES.find(n=>n.title==="zz-keep-me");
  if(keep) await fetch("/api/notes/"+keep.id,{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({_delete:true})});

  // ---- yedek baglantilari
  const links=[...document.querySelectorAll('a[href^="/api/export"],a[href^="/api/backup"]')];
  ok("both backups are offered", links.length===2, links.map(a=>a.getAttribute("href")).join(" "));
  ok("they download rather than navigate", links.every(a=>a.hasAttribute("download")));
  for(const a of links){
    const r=await fetch(a.getAttribute("href"));
    const cd=r.headers.get("content-disposition")||"";
    const blob=await r.blob();
    ok("backup "+a.getAttribute("href")+" downloads",
       r.ok && blob.size>100000 && /attachment; filename="minitoolkit-/.test(cd),
       Math.round(blob.size/1024)+" KB, "+cd.slice(0,44));
  }
  return log;
})()
