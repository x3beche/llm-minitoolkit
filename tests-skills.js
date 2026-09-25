// The skills area: publish a folder, browse it, read its files, install line,
// and delete. Self-contained — it publishes its own fixture, so it runs against
// an empty catalog.
(async () => {
  const log=[], $=s=>document.querySelector(s);
  const ok=(n,c,d="")=>log.push((c?"PASS":"FAIL")+"  "+n+(d?"   ["+d+"]":""));
  const vis=s=>{const e=$(s);return !!e&&!e.classList.contains("hide")&&e.offsetParent!==null};
  const wait=ms=>new Promise(r=>setTimeout(r,ms));
  const NAME="zz-test-skill";
  const mk=(p,t)=>{const f=new File([t],p.split("/").pop(),{type:"text/plain"});
                   f._path="some-folder/"+p; return f;};

  $("#tab-skills").click(); await wait(600);
  ok("the skills tab opens", vis("#view-skills") && !vis("#view-kb"));
  const before=SKILLS.length;

  // ---- publish a folder with subfolders and a shipped wheel
  $("#sk-add").click(); await wait(200);
  ok("publishing has a drop zone", vis("#skdrop"));
  ok("the skill-writing prompt is offered", !!$("#cpskill"));
  await skTake([
    mk("SKILL.md", "---\nname: "+NAME+"\ndescription: >-\n  A throwaway skill the test suite publishes and removes.\n---\n\n# Test skill\n\n## Steps\n\n```sh\necho hi\n```\n\n## Gotchas\n\nnone\n"),
    mk("scripts/flash.sh", "#!/bin/sh\nopenocd -f cfg/stlink.cfg \\\n        -c \"program $1 verify reset exit\"\n"),
    mk("cfg/stlink.cfg", "source [find interface/stlink.cfg]\n"),
    mk("requirements.txt", "pyserial\n"),
    mk("wheels/fake-1.0-py3-none-any.whl", "not a real wheel, just bytes"),
    mk(".hidden/junk.txt", "should be skipped"),
  ]);
  await wait(300);
  ok("the folder name is stripped from the paths",
     SKSTAGED.some(f=>f.path==="SKILL.md") && SKSTAGED.some(f=>f.path==="scripts/flash.sh"),
     SKSTAGED.map(f=>f.path).join(" "));
  ok("dotfolders are skipped", !SKSTAGED.some(f=>f.path.startsWith(".")));
  ok("SKILL.md is sorted first", SKSTAGED[0].path==="SKILL.md");
  ok("a shipped wheel is marked as an offline dep",
     [...document.querySelectorAll("#sklist .ufile")].some(e=>/offline dep/.test(e.textContent)));
  $("#skpub").click();
  for(let i=0;i<300 && !/published/.test($("#skpubmsg").textContent);i++) await wait(100);
  ok("it publishes", /published/.test($("#skpubmsg").textContent), $("#skpubmsg").textContent);
  ok("it reports files and size", /5 file\(s\)/.test($("#skpubmsg").textContent),
     $("#skpubmsg").textContent);
  await wait(600);

  // ---- what got published
  ok("it opens what was published", SKILL?.name===NAME, SKILL?.name);
  const fs=[...document.querySelectorAll("#skfiles .sf .sfp")].map(e=>e.textContent);
  ok("every file is listed", fs.length===5 && fs[0]==="SKILL.md", fs.join(" "));
  ok("subfolders survived", fs.includes("scripts/flash.sh") && fs.includes("cfg/stlink.cfg"));
  ok("the wheel travelled with it", fs.some(f=>/^wheels\/.+\.whl$/.test(f)));
  ok("nothing hidden was published", !fs.some(f=>f.startsWith(".")), fs.join(" "));
  ok("the description came from the front matter",
     /throwaway skill/.test(SKILL.description), SKILL.description);
  ok("its SKILL.md is rendered", $("#skbody").querySelectorAll(".md-h1,.md-h2").length>=2,
     $("#skbody").querySelectorAll(".md-h1,.md-h2").length+" headings");
  ok("its code blocks render", $("#skbody").querySelectorAll("pre.md-code").length>0);
  // ---- the install panel: pick a system and a tool, get the right script
  ok("the system choice is labelled", /your system/i.test($(".inst .opt label").textContent),
     $(".inst .opt label").textContent);
  ok("ubuntu is the default and looks selected",
     $("#os-ubuntu").getAttribute("aria-selected")==="true"
     && getComputedStyle($("#os-ubuntu")).color!==getComputedStyle($("#os-windows")).color);
  ok("the one-liner is a curl pipe on ubuntu",
     /^curl -fsSL http.*\/install\.sh \| sh$/.test($("#skcmd").textContent.trim()),
     $("#skcmd").textContent.trim());
  $("#os-windows").click(); await wait(150);
  ok("windows gets powershell instead",
     /^irm http.*\/install\.ps1 \| iex$/.test($("#skcmd").textContent.trim()),
     $("#skcmd").textContent.trim());
  $("#skmanual").click(); await wait(150);
  ok("the two-step version uses windows commands too",
     /Invoke-WebRequest/.test($("#skcmd2").textContent) && /^python /m.test($("#skcmd2").textContent),
     $("#skcmd2").textContent.split("\n")[1]);
  $("#os-ubuntu").click(); await wait(150);
  ok("switching back restores the shell version", /curl -sO/.test($("#skcmd2").textContent));
  $("#tl-claude").click(); await wait(150);
  ok("choosing one tool is carried in the url", /[?&]tool=claude/.test($("#skcmd").textContent),
     $("#skcmd").textContent.trim());
  ok("and in the two-step flags", /--tool claude/.test($("#skcmd2").textContent),
     $("#skcmd2").textContent.split("\n")[1]);
  ok("and says opencode will not see it", /opencode will not know/.test($("#skwhere").textContent),
     $("#skwhere").textContent.slice(0,60));
  $("#tl-opencode").click(); await wait(150);
  ok("opencode only says the other half is skipped",
     /[?&]tool=opencode/.test($("#skcmd").textContent)
     && /Claude Code will not list it/.test($("#skwhere").textContent),
     $("#skwhere").textContent.slice(0,44));
  $("#tl-both").click(); await wait(150);
  ok("both is the default and carries no flag", !/tool=/.test($("#skcmd").textContent));
  ok("it always says nothing is run", /runs nothing/.test($("#skwhere").textContent));

  // the generated scripts really exist and are shaped right
  for(const [ext,needle] of [["install.sh","#!/bin/sh"],
                             ["install.ps1","$ErrorActionPreference"]]){
    const r=await fetch(`/api/skill/${NAME}/${ext}`);
    const t=await r.text();
    ok(ext+" is generated", r.ok && t.includes(needle) && t.includes(NAME),
       t.split("\n")[0].slice(0,44));
    // the address it bakes in is the one other machines can reach, which is not
    // necessarily the one this page was opened on
    ok(ext+" bakes in a reachable address",
       /https?:\/\/[^\/\s'"]+\/minitoolkit\.py/.test(t),
       (t.match(/https?:\/\/[^\/\s'"]+/)||[""])[0]);
    ok(ext+" says nothing is run from the skill", /runs nothing from the skill/.test(t));
  }
  {
    const t=await fetch(`/api/skill/${NAME}/install.sh?tool=claude`).then(r=>r.text());
    ok("the script honours the tool choice", /--tool claude/.test(t));
  }

  ok("the two-step version installs the wheels",
     $("#skcmd2").textContent.split("\n").some(l=>
       l.trim()===`python3 minitoolkit.py skill install ${NAME} --with-wheels`),
     $("#skcmd2").textContent.replace(/\n/g," ⏎ "));
  ok("it shows how to get the client first",
     /curl -sO .*\/minitoolkit\.py/.test($("#skcmd2").textContent));
  $("#skplanbtn").click(); await wait(200);
  ok("it says what an install writes", vis("#skplan")
     && /\.claude\/skills\/.+SKILL\.md/.test($("#skplanbody").textContent),
     $("#skplanbody").textContent.split("\n")[0]);
  ok("the plan mentions opencode too",
     /opencode\/AGENTS\.md/.test($("#skplanbody").textContent));
  ok("the plan spells out the offline pip line",
     /--no-index/.test($("#skplanbody").textContent));
  $("#skplanbtn").click(); await wait(150);
  ok("the plan folds away again", !vis("#skplan"));
  ok("it says nothing is fetched",
     vis("#skoffline") && /nothing is fetched/.test($("#skoffline").textContent),
     $("#skoffline").textContent);
  ok("a zip is offered", ($("#skzip").getAttribute("href")||"").endsWith(NAME+".zip"));
  ok("the zip link looks like the other buttons",
     getComputedStyle($("#skzip")).borderStyle==="solid"
     && getComputedStyle($("#skzip")).textDecorationLine==="none",
     getComputedStyle($("#skzip")).borderStyle+" / "+getComputedStyle($("#skzip")).textDecorationLine);

  // ---- reading a file
  const sf=[...document.querySelectorAll("#skfiles .sf")]
    .find(e=>e.querySelector(".sfp").textContent==="scripts/flash.sh");
  sf.click(); await wait(500);
  ok("clicking a file shows it", vis("#skfile") && /openocd/.test($("#skfile").textContent),
     $("#skfile").textContent.slice(0,36).replace(/\n/g,"⏎"));
  ok("its indentation survives the round trip", /\n        -c /.test($("#skfile").textContent));
  ok("it is labelled with its type", $("#skfile").getAttribute("data-lang")==="sh");
  document.querySelectorAll("#skfiles .sf")[0].click(); await wait(300);
  ok("SKILL.md switches back to the rendered view", vis("#skbody") && !vis("#skfile"));

  // ---- catalog
  $("#skcrumb").querySelector("a").click(); await wait(400);
  ok("the crumb walks back", vis("#sk-catalog"));
  ok("the catalog gained it", SKILLS.length===before+1,
     before+" -> "+SKILLS.length+" · "+$("#skcount").textContent);
  ok("a card says how many files it has",
     [...document.querySelectorAll("#skgrid .cbadge")].some(e=>/5 files/.test(e.textContent)));
  $("#skfilter").value="throwaway"; renderSkGrid(); await wait(150);
  ok("the catalog filters on description",
     document.querySelectorAll("#skgrid .card").length===1, $("#skcount").textContent);
  $("#skfilter").value=""; renderSkGrid(); await wait(100);

  // ---- zip really contains it
  const z=await fetch("/api/skill/"+NAME+".zip");
  const blob=await z.blob();
  ok("the zip downloads", z.ok && blob.size>200, blob.size+" bytes");

  // ---- delete
  await openSkill(NAME); await wait(400);
  $("#skdel").click(); await wait(100); $("#skdel").click();
  for(let i=0;i<100 && SKILL;i++) await wait(100);
  ok("a skill can be deleted", /deleted/.test($("#skmsg").textContent), $("#skmsg").textContent);
  ok("it leaves the catalog", !SKILLS.some(s=>s.name===NAME) && SKILLS.length===before,
     SKILLS.length+" skills");
  return log;
})()
