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
        <h2>Sign in</h2>
        <label for="ma_user">Username</label>
        <input id="ma_user" autocomplete="username">
        <label for="ma_pass">Password</label>
        <input id="ma_pass" type="password" autocomplete="current-password">
        <div id="ma_err"></div>
        <div class="ma_row">
          <button id="ma_go">Sign in</button>
          <button class="ghost" id="ma_cancel">Cancel</button>
        </div>
        <p class="ma_hint">No account? Create one on the server with
          <code>python scripts/manage_users.py add &lt;name&gt;</code></p>
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
      #ma_box #ma_err { color:#b3442f; font-size:12px; margin-top:7px;
                        overflow-wrap:anywhere; }
      #ma_box .ma_hint { font-size:11px; color:#5c6b7a; margin:11px 0 0; }
      #ma_box code { font:11px ui-monospace,Consolas,monospace; }`;
    document.head.appendChild(style);

    $("ma_cancel").onclick = close;
    $("ma_go").onclick = submit;
    wrap.addEventListener("keydown", e => {
      if (e.key === "Escape") close();
      if (e.key === "Enter") submit();
    });
    wrap.addEventListener("click", e => { if (e.target === wrap) close(); });
  }

  function open_() {
    buildModal();
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
      // Not through the page's api() helper: login lives outside
      // /v1/users/{user} and takes no credential.
      const res = await fetch("/v1/auth/login", {
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

  return {install, headers, remember, refreshWho, open: open_, signOut};
})();
