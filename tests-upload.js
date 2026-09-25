(async () => {
  const log=[], $=s=>document.querySelector(s);
  const ok=(n,c,d="")=>log.push((c?"PASS":"FAIL")+"  "+n+(d?"   ["+d+"]":""));
  const vis=s=>{const e=$(s);return !!e&&!e.classList.contains("hide")&&e.offsetParent!==null};
  const wait=ms=>new Promise(r=>setTimeout(r,ms));
  const DOCS={"uart-dma.md": "# STM32 UART over DMA — Idle-Line Reception\n\ntags: stm32 uart dma idle-line ringbuffer\n\nReceive variable-length UART frames on an STM32L4 without polling or a byte\ninterrupt per character, using DMA in circular mode plus the IDLE line event.\n\n## 01 Why idle-line detection\n\nA byte-per-interrupt receiver drops frames the moment the bus gets busy.\n\n| Approach | Cost per byte | Frame boundary |\n| --- | --- | --- |\n| RXNE interrupt | one ISR | counted by hand |\n| DMA + IDLE | none | hardware |\n\n## 02 Setting it up\n\n```c\nvoid uart_dma_start(void)\n{\n    LL_USART_EnableDMAReq_RX(USART2);\n    LL_USART_EnableIT_IDLE(USART2);          /* the whole trick */\n}\n```\n\n> **GOTCHA — clearing IDLE**\n>\n> Reading ISR alone does not clear IDLE; you must write ICR.IDLECF or the\n> interrupt re-fires forever.\n\n## Gotchas & common mistakes\n\n- Circular DMA never stops, so NDTR must be read, not assumed.\n\n## Sources\n\n- https://example.invalid/rm0432  reference manual, USART chapter\n", "frontmatter.md": "---\ntitle: Yocto — bitbake temel komutları\ntags: [yocto, bitbake, gömülü]\n---\n\nBir imajı sıfırdan kurup hata ayıklamak için günlük kullanılan bitbake komutları.\n\n## 01 Temel akış\n\n```sh\nsource oe-init-build-env\nbitbake core-image-minimal\n```\n", "empty.md": "# Empty one\n\ntags: x\n", "plain.txt": "no title here, just prose about something.\n\nsecond paragraph.\n"};
  const file=(n)=>new File([DOCS[n]], n, {type:"text/markdown"});

  $("#tab-kb").click(); $("#sub-add").click(); await wait(300);
  ok("the writing prompt button is there", !!$("#cpwrite"));
  ok("the drop zone is there", vis("#udrop"));

  // --- gercek bir drop olayi
  const dt=new DataTransfer(); dt.items.add(file("uart-dma.md"));
  $("#udrop").dispatchEvent(new DragEvent("drop",{dataTransfer:dt,bubbles:true,cancelable:true}));
  await wait(500);
  ok("dropping a file stages it", STAGED.length===1, STAGED.length+" staged");
  ok("the title comes from the h1", $("#n-title").value==="STM32 UART over DMA — Idle-Line Reception",
     $("#n-title").value);
  ok("tags come from the tags line", $("#n-tags").value==="stm32 uart dma idle-line ringbuffer",
     $("#n-tags").value);
  ok("the title line is not left in the body", !/^#\s/m.test($("#n-body").value.split("\n")[0]),
     $("#n-body").value.split("\n")[0].slice(0,40));
  ok("the tags line is not left in the body", !/^tags:/mi.test($("#n-body").value));
  ok("the summary survives", /variable-length UART frames/.test($("#n-body").value));
  ok("code indentation survives", /\n    LL_USART_EnableDMAReq_RX/.test($("#n-body").value));
  ok("the table survives", /\| --- \| --- \|/.test($("#n-body").value));

  // --- front matter
  const dt2=new DataTransfer(); dt2.items.add(file("frontmatter.md"));
  $("#udrop").dispatchEvent(new DragEvent("drop",{dataTransfer:dt2,bubbles:true,cancelable:true}));
  await wait(400);
  const fm=STAGED.find(d=>d.name==="frontmatter.md");
  ok("front matter title is read", fm?.title==="Yocto — bitbake temel komutları", fm?.title);
  ok("front matter tags are normalised", fm?.tags==="yocto bitbake gömülü", fm?.tags);
  ok("front matter is stripped from the body", !/^---/.test(fm?.body||"x"), (fm?.body||"").slice(0,20));

  // --- basliksiz duz metin
  const dt3=new DataTransfer(); dt3.items.add(new File([DOCS["plain.txt"]],"plain.txt",{type:"text/plain"}));
  $("#udrop").dispatchEvent(new DragEvent("drop",{dataTransfer:dt3,bubbles:true,cancelable:true}));
  await wait(400);
  const pl=STAGED.find(d=>d.name==="plain.txt");
  ok("a file with no title falls back to its name", pl?.title==="plain", pl?.title);
  ok("its whole text is kept", /second paragraph/.test(pl?.body||""));

  // --- bos govde reddedilsin
  const dt4=new DataTransfer(); dt4.items.add(file("empty.md"));
  $("#udrop").dispatchEvent(new DragEvent("drop",{dataTransfer:dt4,bubbles:true,cancelable:true}));
  await wait(400);
  const em=STAGED.find(d=>d.name==="empty.md");
  ok("an empty document is flagged, not saved", !!em?.why, em?.why);
  ok("it is marked in the list", !!document.querySelector(".ufile.bad"));

  ok("save all appears for several documents", vis("#saveall"), STAGED.length+" staged");
  const before=(await fetch("/api/status").then(r=>r.json())).kb.notes;
  $("#saveall").click();
  for(let i=0;i<120 && $("#saveall").disabled;i++) await wait(100);
  const after=(await fetch("/api/status").then(r=>r.json())).kb.notes;
  ok("only the good ones are saved", after-before===3, before+" -> "+after);
  ok("the list is cleared afterwards", STAGED.length===0 && !vis("#saveall"), $("#savemsg").textContent);

  // --- kaydedilen markdown olarak aciliyor mu
  const all=(await fetch("/api/notes?limit=1000").then(r=>r.json())).notes;
  const saved=all.find(n=>n.title==="STM32 UART over DMA — Idle-Line Reception");
  ok("the saved document is in the library", !!saved, "#"+saved?.id);
  await openNote(saved.id); await wait(700);
  ok("it renders with its table and code",
     $("#gbody").querySelectorAll("table.md-table").length===1
     && $("#gbody").querySelectorAll("pre.md-code").length===1
     && $("#gbody").querySelectorAll("blockquote.md-quote").length===1,
     [...$("#gbody").querySelectorAll("table.md-table,pre.md-code,blockquote.md-quote")].length+" blocks");
  ok("it landed under own notes", DECK==="own", DECK);

  // --- temizlik
  for(const t of ["STM32 UART over DMA — Idle-Line Reception","Yocto — bitbake temel komutları","plain"]){
    const n=all.find(x=>x.title===t);
    if(n) await fetch("/api/notes/"+n.id,{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({_delete:true})});
  }
  const end=(await fetch("/api/status").then(r=>r.json())).kb.notes;
  ok("test documents cleaned up", end===before, before+" -> "+end);
  return log;
})()