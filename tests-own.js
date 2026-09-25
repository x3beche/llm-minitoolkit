(async () => {
  const log=[], $=s=>document.querySelector(s);
  const ok=(n,c,d="")=>log.push((c?"PASS":"FAIL")+"  "+n+(d?"   ["+d+"]":""));
  const vis=s=>{const e=$(s);return !!e&&!e.classList.contains("hide")&&e.offsetParent!==null};
  const wait=ms=>new Promise(r=>setTimeout(r,ms));
  $("#tab-kb").click(); $("#sub-add").click(); await wait(300);
  $("#n-title").value="UI test note — markdown check";
  $("#n-tags").value="uitest";
  $("#n-body").value="## A heading\n\nSome prose with `code` in it.\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n\n```sh\necho hi\n```";
  $("#save").click(); await wait(1200);
  ok("a note saves", /saved|updated/.test($("#savemsg").textContent), $("#savemsg").textContent);

  $("#sub-list").click(); await wait(1000);
  const own=[...document.querySelectorAll("#decks .ct")].find(e=>e.textContent==="own notes");
  ok("own notes deck exists", !!own);
  own.closest(".card").click(); await wait(700);
  const card=[...document.querySelectorAll("#notes .card")]
    .find(c=>c.querySelector(".ct").textContent.includes("UI test note"));
  ok("the new note is in own notes", !!card, document.querySelectorAll("#notes .card").length+" notes");
  card.click(); await wait(700);
  ok("it renders as markdown",
     $("#gbody").querySelector(".md-h1,.md-h2")?.textContent==="A heading"
     && $("#gbody").querySelectorAll("table.md-table").length===1
     && $("#gbody").querySelectorAll("pre.md-code").length===1,
     [$("#gbody").querySelectorAll(".md-h1,.md-h2").length,
      $("#gbody").querySelectorAll("table.md-table").length,
      $("#gbody").querySelectorAll("pre.md-code").length].join("/"));
  ok("own notes have no source link", !/source ↗/.test($("#gmeta").innerHTML));

  // temizle
  const b=$("#g-del"); b.click(); await wait(100); b.click(); await wait(1200);
  ok("delete returns to the deck", vis("#lib-deck")||vis("#lib-catalog"));
  return log;
})()
