// パスキー（WebAuthn）: ログイン画面の「パスキーでログイン」と、ログイン設定画面の登録。
// サーバーとは JSON でやり取りする。バイナリは base64url の文字列にして送る。
(() => {
  if (!window.PublicKeyCredential || !navigator.credentials) return;   // 対応していなければトークンだけ

  const toBytes = (s) => {
    const b64 = s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4);
    return Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
  };
  const toB64u = (buf) => {
    let s = "";
    new Uint8Array(buf).forEach((b) => (s += String.fromCharCode(b)));
    return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  };
  const csrf = () => document.querySelector("meta[name=csrf]")?.content || "";

  const post = async (url, body) => {
    const r = await fetch(url, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || `エラー（${r.status}）`);
    return data;
  };

  // 端末で取り消した・時間切れ（NotAllowedError）は失敗扱いにしない
  const message = (err) => (err && err.name === "NotAllowedError" ? "取り消されました" : (err && err.message) || String(err));

  const credentialJSON = (cred) => {
    const r = cred.response;
    const response = { clientDataJSON: toB64u(r.clientDataJSON) };
    if (r.attestationObject) {
      response.attestationObject = toB64u(r.attestationObject);
      response.transports = r.getTransports ? r.getTransports() : [];
    } else {
      response.authenticatorData = toB64u(r.authenticatorData);
      response.signature = toB64u(r.signature);
      if (r.userHandle) response.userHandle = toB64u(r.userHandle);
    }
    return {
      id: cred.id, rawId: toB64u(cred.rawId), type: cred.type, response,
      authenticatorAttachment: cred.authenticatorAttachment || undefined,
      clientExtensionResults: cred.getClientExtensionResults ? cred.getClientExtensionResults() : {},
    };
  };

  const bind = (box, run) => {
    box.hidden = false;
    const btn = box.querySelector("button");
    const status = box.querySelector("[data-passkey-status]");
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      status.textContent = "端末の確認を待っています…";
      try {
        await run(status);
      } catch (err) {
        status.textContent = message(err);
      } finally {
        btn.disabled = false;
      }
    });
  };

  const login = document.querySelector("[data-passkey-login]");
  if (login) {
    bind(login, async () => {
      const { request, options } = await post("/login/passkey/options", {});
      const publicKey = {
        ...options,
        challenge: toBytes(options.challenge),
        allowCredentials: (options.allowCredentials || []).map((c) => ({ ...c, id: toBytes(c.id) })),
      };
      const cred = await navigator.credentials.get({ publicKey });
      const { redirect } = await post("/login/passkey", { request, credential: credentialJSON(cred), next: login.dataset.next });
      location.assign(redirect);
    });
  }

  const register = document.querySelector("[data-passkey-register]");
  if (register) {
    bind(register, async (status) => {
      const { request, options } = await post("/account/passkeys/options", { csrf: csrf() });
      const publicKey = {
        ...options,
        challenge: toBytes(options.challenge),
        user: { ...options.user, id: toBytes(options.user.id) },
        excludeCredentials: (options.excludeCredentials || []).map((c) => ({ ...c, id: toBytes(c.id) })),
      };
      const cred = await navigator.credentials.create({ publicKey });
      const name = register.querySelector("input[name=passkey-name]").value;
      await post("/account/passkeys", { csrf: csrf(), request, credential: credentialJSON(cred), name });
      status.textContent = "登録しました";
      location.assign("/account?" + new URLSearchParams({ ok: "パスキーを登録しました" }));
    });
  }
})();
