// Burned-in captions + corner badge, injected into every recorded page (stage cards and the live web UI).
(() => {
  function ensure() {
    if (!document.getElementById("cap")) {
      const st = document.createElement("style");
      st.textContent = `#cap{position:fixed;left:50%;bottom:54px;transform:translateX(-50%);max-width:1560px;padding:16px 30px;
        border-radius:14px;background:rgba(0,0,0,.78);color:#fff;font:600 36px/1.35 ui-sans-serif,system-ui,-apple-system,sans-serif;
        text-align:center;z-index:99999;display:none;zoom:1}
        #badge{position:fixed;top:26px;right:30px;font:500 20px ui-sans-serif,system-ui,sans-serif;color:#cfcac0;
        background:rgba(0,0,0,.55);padding:7px 14px;border-radius:999px;z-index:99999;display:none}`;
      document.head.appendChild(st);
      for (const id of ["cap", "badge"]) {
        const d = document.createElement("div");
        d.id = id;
        document.body.appendChild(d);
      }
    }
  }
  // cues: [{t: seconds from now, text}], last cue hides at `end` seconds
  window.__captions = (cues, end) => {
    ensure();
    const cap = document.getElementById("cap");
    for (const c of cues) setTimeout(() => { cap.textContent = c.text; cap.style.display = "block"; }, c.t * 1000);
    if (end) setTimeout(() => { cap.style.display = "none"; }, end * 1000);
  };
  window.__badge = (text) => {
    ensure();
    const b = document.getElementById("badge");
    b.textContent = text;
    b.style.display = text ? "block" : "none";
  };
})();
