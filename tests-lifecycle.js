(async () => {
  const log=[], $=s=>document.querySelector(s);
  const ok=(n,c,d="")=>log.push((c?"PASS":"FAIL")+"  "+n+(d?"   ["+d+"]":""));
  const vis=s=>{const e=$(s);return !!e&&!e.classList.contains("hide")&&e.offsetParent!==null};
  const wait=ms=>new Promise(r=>setTimeout(r,ms));
  $("#tab-skills").click(); await wait(800);
  await openSkill("uart-tool"); await wait(600);

  ok("a 'then use it' panel is there", vis("#skuse") && $("#skusebody").children.length>0);
  const use=$("#skusebody").textContent;
  ok("it explains opencode", /opencode/.test(use) && /AGENTS\.md/.test(use));
  ok("it shows an opencode session", /opencode\n>/.test($("#skusebody").innerText||use));
  ok("it explains Claude Code", /Claude Code/.test(use) && /\.claude\/skills/.test(use));
  ok("it names the skill as a command", new RegExp("/uart-tool").test(use));
  ok("the example is a phrase, not one word",
     ($("#skusebody").querySelector("pre").textContent.split("\n")[1]||"").length>12,
     $("#skusebody").querySelector("pre").textContent.split("\n")[1]);
  ok("it says neither tool is required", /Neither tool is required/.test(use));

  ok("a management panel is there", !!$("#skmanage"));
  const mg=$("#skmanage").textContent;
  ok("it lists installed / disable / enable / uninstall",
     /skill installed/.test(mg) && /skill disable uart-tool/.test(mg)
     && /skill enable uart-tool/.test(mg) && /skill uninstall uart-tool/.test(mg));
  ok("it says what disable keeps", /keeps the files/.test($("#skmanage").parentElement.textContent));

  $("#tl-claude").click(); await wait(200);
  ok("choosing Claude Code only drops the opencode half",
     !/opencode —/.test($("#skusebody").textContent) && /Claude Code/.test($("#skusebody").textContent));
  $("#tl-opencode").click(); await wait(200);
  ok("choosing opencode only drops the Claude half",
     /opencode/.test($("#skusebody").textContent) && !/Claude Code —/.test($("#skusebody").textContent));
  $("#os-windows").click(); await wait(200);
  ok("windows uses python, not python3", /^python minitoolkit/m.test($("#skmanage").textContent),
     $("#skmanage").textContent.split("\n")[0]);
  $("#os-ubuntu").click(); $("#tl-both").click(); await wait(200);
  ok("back to both shows both again",
     /opencode/.test($("#skusebody").textContent) && /Claude Code/.test($("#skusebody").textContent));
  return log;
})()
