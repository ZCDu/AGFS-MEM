/*
 * Shared sign-in for /chat and /gui.
 *
 * Served as a static file rather than duplicated into both pages: the two UIs
 * previously drifted — the graph editor grew a sign-in modal and the chat page
 * never did, so chat users had to obtain a token with curl and paste it in by
 * hand. One implementation cannot drift from itself.
 *
 * Both pages already had `user` and `token` inputs; this attaches to those and
 * adds the modal, the /v1/auth/login call, and the credential readout. A page
 * opts in with:
 *
 *     MemAuth.install({ onSignedIn: () => reload() });
 *
 * WHY sessionStorage AND NOT localStorage
 *   A session token is a bearer credential. sessionStorage is scoped to the
 *   tab and cleared when it closes, which is the right lifetime for something
 *   typed into a dev tool on a shared machine. localStorage would persist it
 *   indefinitely with no sign-out.
 */

const MemAuth = (() => {
  const $ = id => document.getElementById(id);
  let onSignedIn = () => {};

  function headers() {
    const el = $("token");
    const t = el ? el.value.trim() : "";
    return t ? {"Authorization": "Bearer " + t} : {};
  }

  function remember() {
    try {
      sessionStorage.setItem("memtoken", ($("token") || {}).value || "");
      sessionStorage.setItem("memuser", ($("user") || {}).value || "");
    } catch { /* private browsing can block storage; not worth failing over */ }
  }

  function restore() {
    try {
      const t = sessionStorage.getItem("memtoken");
      const u = sessionStorage.getItem("memuser");
      if (t && $("token")) $("token").value = t;
      if (u && $("user")) $("user").value = u;
    } catch { /* ignore */ }
  }

  async function refreshWho() {
    const el = $("who");
    if (!el) return;
    const t = ($("token") || {}).value?.trim();
    if (!t) {
      // Distinguish "no credential needed" from "no credential supplied":
      // with AUTH_MODE=off an empty token box is correct, and saying nothing
      // leaves the user wondering whether they forgot something.
      try {
        const r = await fetch("/v1/auth/me");
        if (r.ok && (await r.json()).auth === "off") {
          el.textContent = "auth off"; return;
        }
      } catch { /* ignore */ }
      el.textContent = "not signed in";
      return;
    }
    try {
      const res = await fetch("/v1/auth/me", {headers: {"Authorization": "Bearer " + t}});
      if (!res.ok) { el.textContent = "credential rejected"; return; }
      const me = await res.json();
      el.textContent = me.auth === "off" ? "auth off"
        : `${me.auth}${me.is_admin ? " · admin" : ""}${me.user_id ? " · " + me.user_id : ""}`;
    } catch { el.textContent = ""; }
  }

  function buildModal() {
    if ($("ma_box")) return;
    const wrap = document.createElement("div");
    wrap.id = "ma_box";
    wrap.hidden = true;
    wrap.innerHTML = `
      <div class="ma_card">
        <h2 id="ma_title">Sign in</h2>
        <label for="ma_user">Username</label>
        <input id="ma_user" autocomplete="username">
        <label for="ma_pass">Password</label>
        <input id="ma_pass" type="password" autocomplete="current-password">
        <div id="ma_err"></div>
        <div class="ma_row">
          <button id="ma_go">Sign in</button>
          <button class="ghost" id="ma_cancel">Cancel</button>
        </div>
        <button class="ma_link" id="ma_toggle">Create an account instead</button>
      </div>`;
    document.body.appendChild(wrap);

    const style = document.createElement("style");
    style.textContent = `
      /* display lives here, never in an inline style: [hidden] is only a
         user-agent rule and an inline display would outrank it, leaving a
         full-screen overlay permanently on top of the page swallowing every
         click. That shipped once already. */
      #ma_box { position:fixed; inset:0; z-index:40; background:rgba(22,32,43,.45);
                display:grid; place-items:center; }
      #ma_box[hidden] { display:none; }
      #ma_box .ma_card { background:#fff; border:1px solid #ccd5de; border-radius:4px;
                         padding:20px; width:300px; font:14px "Segoe UI",system-ui,sans-serif; }
      #ma_box h2 { font:600 11px "Segoe UI",system-ui,sans-serif; letter-spacing:.09em;
                   text-transform:uppercase; color:#5c6b7a; margin:0 0 10px; }
      #ma_box label { display:block; font-size:11px; color:#5c6b7a; margin:8px 0 3px; }
      #ma_box input { width:100%; padding:6px 8px; border:1px solid #ccd5de;
                      border-radius:3px; font:14px inherit; }
      #ma_box .ma_row { display:flex; gap:6px; margin-top:11px; }
      #ma_box button { flex:1; padding:7px 10px; border-radius:3px; cursor:pointer;
                       background:#16202b; color:#fff; border:1px solid #16202b;
                       font-weight:600; }
      #ma_box button.ghost { background:#fff; color:#16202b; border-color:#ccd5de;
                             font-weight:500; }
      #ma_box .ma_link { background:none; color:#2f4fd4; border:none; width:100%;
                         text-align:center; font-weight:500; font-size:12px;
                         padding:8px 0 2px; margin-top:6px; }
      #ma_box .ma_link:hover { text-decoration:underline; }
      #ma_box #ma_err { color:#b3442f; font-size:12px; margin-top:7px;
                        overflow-wrap:anywhere; }`;
    document.head.appendChild(style);

    $("ma_cancel").onclick = close;
    $("ma_go").onclick = submit;
    $("ma_toggle").onclick = toggleMode;
    wrap.addEventListener("keydown", e => {
      if (e.key === "Escape") close();
      if (e.key === "Enter") submit();
    });
    wrap.addEventListener("click", e => { if (e.target === wrap) close(); });
  }

  let mode = "login";   // "login" | "register"

  function toggleMode() {
    mode = mode === "login" ? "register" : "login";
    const title = $("ma_title");
    const go = $("ma_go");
    const toggle = $("ma_toggle");
    const pass = $("ma_pass");
    if (mode === "register") {
      title.textContent = "Create account";
      go.textContent = "Create account";
      toggle.textContent = "I already have an account — sign in";
      pass.setAttribute("autocomplete", "new-password");
    } else {
      title.textContent = "Sign in";
      go.textContent = "Sign in";
      toggle.textContent = "Create an account instead";
      pass.setAttribute("autocomplete", "current-password");
    }
    $("ma_err").textContent = "";
    $("ma_pass").value = "";
    $("ma_user").focus();
  }

  function open_() {
    buildModal();
    if (mode !== "login") toggleMode();   // always land on the login view
    $("ma_err").textContent = "";
    $("ma_user").value = ($("user") || {}).value || "";
    $("ma_pass").value = "";
    $("ma_box").hidden = false;
    $("ma_user").focus();
  }

  function close() {
    const box = $("ma_box");
    if (box) { box.hidden = true; $("ma_pass").value = ""; }
  }

  async function submit() {
    const username = $("ma_user").value.trim();
    const password = $("ma_pass").value;
    if (!username || !password) {
      $("ma_err").textContent = "Both fields are required."; return;
    }
    $("ma_go").disabled = true;
    try {
      const isReg = mode === "register";
      const res = await fetch(isReg ? "/v1/auth/register" : "/v1/auth/login", {
        method: "POST", headers: {"content-type": "application/json"},
        body: JSON.stringify({username, password}),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        $("ma_err").textContent = data.detail || `HTTP ${res.status}`;
        return;
      }
      if ($("token")) $("token").value = data.token;
      // Follow the account to the namespace it can actually reach. Leaving the
      // user field alone would give a 403 on the next request, which reads as
      // a broken login rather than a mismatched field.
      if ($("user")) $("user").value = data.user_id;
      remember();
      close();
      await refreshWho();
      onSignedIn(data);
    } catch (e) {
      $("ma_err").textContent = e.message;
    } finally {
      $("ma_go").disabled = false;
    }
  }

  function signOut() {
    if ($("token")) $("token").value = "";
    remember();
    refreshWho();
  }

  /* ---------- wiki access / join ----------
     A shared control for both UIs: without one you can inspect storage but
     not reach the meeting/project wikis other people have invited you to.
     Joining is self-service with a passcode — the passcode is the
     authorisation, so no admin approval round-trip is needed. */
  function listWikis() {
    return fetch("/v1/wikis", {headers: headers()})
      .then(async r => {
        const d = await r.json().catch(() => []);
        if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
        return Array.isArray(d) ? d : [];
      });
  }

  function joinModal(opts = {}) {
    const onJoined = opts.onJoined || (() => {});
    const joinStyle = document.createElement("style");
    joinStyle.textContent = `
      #ms_box { position:fixed; inset:0; z-index:41; background:rgba(22,32,43,.45);
                display:grid; place-items:center; }
      #ms_box[hidden] { display:none; }
      #ms_box .ma_card { background:#fff; border:1px solid #ccd5de; border-radius:4px;
                         padding:20px; width:320px; font:14px "Segoe UI",system-ui,sans-serif; }
      #ms_box h2 { font:600 11px "Segoe UI",system-ui,sans-serif; letter-spacing:.09em;
                   text-transform:uppercase; color:#5c6b7a; margin:0 0 10px; }
      #ms_box label { display:block; font-size:11px; color:#5c6b7a; margin:8px 0 3px; }
      #ms_box input { width:100%; padding:6px 8px; border:1px solid #ccd5de;
                      border-radius:3px; font:14px inherit; }
      #ms_box #ms_err { color:#b3442f; font-size:12px; margin-top:7px;
                        overflow-wrap:anywhere; }
      #ms_box .ma_row { display:flex; gap:6px; margin-top:11px; }
      #ms_box button { flex:1; padding:7px 10px; border-radius:3px; cursor:pointer;
                       background:#16202b; color:#fff; border:1px solid #16202b;
                       font-weight:600; }
      #ms_box button.ghost { background:#fff; color:#16202b; border-color:#ccd5de;
                             font-weight:500; }`;
    document.head.appendChild(joinStyle);
    const box = document.createElement("div");
    box.id = "ms_box";
    box.innerHTML = `
      <div class="ma_card">
        <h2>Join a wiki</h2>
        <label>Wiki id</label>
        <input id="ms_wiki" placeholder="e.g. q3-project-planning">
        <label>Invite passcode</label>
        <input id="ms_code" placeholder="8-character passcode from the owner">
        <div id="ms_err"></div>
        <div class="ma_row">
          <button id="ms_go">Join</button>
          <button class="ghost" id="ms_cancel">Cancel</button>
        </div>
      </div>`;
    document.body.appendChild(box);
    const $s = id => box.querySelector("#" + id);
    function closeJ() {
      box.hidden = true; $s("ms_code").value = "";
    }
    $s("ms_cancel").onclick = closeJ;
    box.addEventListener("click", e => { if (e.target === box) closeJ(); });
    $s("ms_go").onclick = async () => {
      const wiki = $s("ms_wiki").value.trim();
      const code = $s("ms_code").value.trim();
      if (!wiki || !code) { $s("ms_err").textContent = "Both fields are required."; return; }
      $s("ms_go").disabled = true;
      try {
        const r = await fetch(`/v1/wikis/${encodeURIComponent(wiki)}/join`, {
          method: "POST", headers: {"content-type": "application/json", ...headers()},
          body: JSON.stringify({passcode: code}),
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) { $s("ms_err").textContent = d.detail || `HTTP ${r.status}`; return; }
        closeJ();
        onJoined(d);
      } catch (e) {
        $s("ms_err").textContent = e.message;
      } finally {
        $s("ms_go").disabled = false;
      }
    };
    box.hidden = false;
    $s("ms_wiki").focus();
    return {close: closeJ};
  }

  /* ---------- file upload / processing ----------
     The server stores raw files and extracts text on demand; this adds the
     UI half. A dropped file is uploaded (multipart), its text is fetched, and
     the caller decides what to do with it (usually feed it to /extract so it
     can be reviewed before any memory is written). */
  function upload(userId, file, sessionId) {
    const fd = new FormData();
    fd.append("file", file);
    if (sessionId) fd.append("session_id", sessionId);
    return fetch(`/v1/users/${encodeURIComponent(userId)}/files`, {
      method: "POST", headers: headers(), body: fd,
    }).then(async r => {
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
      return d;
    });
  }

  function fileText(userId, fileId) {
    return fetch(`/v1/users/${encodeURIComponent(userId)}/files/${encodeURIComponent(fileId)}/text`, {
      headers: headers(),
    }).then(async r => {
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
      return d;
    });
  }

  function installDnD(opts = {}) {
    // opts: { accept, acceptAll, onDrop({file,userId,fileId,name,text,extractable}) }
    const accept = (opts.accept || ".txt,.md,.markdown,.csv,.log,.json").toLowerCase();
    const overlay = document.createElement("div");
    overlay.id = "ma_dropoverlay";
    const inner = document.createElement("div");
    inner.className = "inner";
    inner.textContent = "Drop file(s) to extract into memory";
    overlay.appendChild(inner);
    const style = document.createElement("style");
    style.textContent = `
      #ma_dropoverlay { position:fixed; inset:0; z-index:60; place-items:center;
        background:rgba(47,79,212,.12); display:none; }
      #ma_dropoverlay.on { display:grid; }
      #ma_dropoverlay .inner { font:600 15px "Segoe UI",system-ui,sans-serif;
        color:var(--accent,#2f4fd4); background:#fff; padding:14px 24px;
        border:3px dashed var(--accent,#2f4fd4); border-radius:6px;
        box-shadow:0 4px 14px rgba(0,0,0,.12); }`;
    document.head.appendChild(style);
    document.body.appendChild(overlay);

    let depth = 0;
    const matches = f => {
      const ext = "." + (f.name.split(".").pop() || "").toLowerCase();
      return opts.acceptAll || /^text\//.test(f.type) || f.type === "application/json"
        || accept.split(",").some(s => s.trim() === ext);
    };
    document.addEventListener("dragenter", e => {
      e.preventDefault(); depth++; overlay.classList.add("on");
    });
    document.addEventListener("dragover", e => { e.preventDefault(); if (e.dataTransfer) e.dataTransfer.dropEffect = "copy"; });
    document.addEventListener("dragleave", e => {
      e.preventDefault(); if (--depth <= 0) { depth = 0; overlay.classList.remove("on"); }
    });
    document.addEventListener("drop", e => {
      e.preventDefault(); depth = 0; overlay.classList.remove("on");
      const files = e.dataTransfer && e.dataTransfer.files;
      if (!files || !files.length) return;
      Array.from(files).filter(matches).forEach(f => {
        if (opts.onDrop) opts.onDrop(f);
      });
    });
  }

  function install(opts = {}) {
    onSignedIn = opts.onSignedIn || (() => {});
    restore();
    buildModal();
    const btn = $("signin");
    if (btn) btn.onclick = open_;
    const out = $("signout");
    if (out) out.onclick = signOut;
    for (const id of ["token", "user"]) {
      const el = $(id);
      if (el) el.addEventListener("change", () => { remember(); refreshWho(); });
    }
    refreshWho();
  }

  return {install, headers, remember, refreshWho, open: open_, signOut,
          listWikis, joinModal, upload, fileText, installDnD};
})();
