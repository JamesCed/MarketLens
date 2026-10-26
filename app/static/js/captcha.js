/* =====================================================================
   HUMAN VERIFICATION (invisible CAPTCHA)
   =====================================================================
   Server side: app/utils/captcha.py. window.DSS_CAPTCHA is written by
   shared/_captcha.html: {enabled, provider: "turnstile"|"recaptcha", siteKey}.

   1. Forms marked data-captcha="<action>" (sign-in, sign-up, forgot
      password, new plan) get a token just before they submit, in a
      hidden captcha_token field.
   2. A same-origin fetch() answered with 403 and "X-Captcha-Required: 1"
      (the AI and Google Places routes) is retried once, after a token
      has been posted to /api/captcha/verify for a short session pass.

   Cloudflare Turnstile runs with appearance "interaction-only": nothing
   is shown unless Cloudflare needs a click, and then the small widget
   appears in the bottom-right corner. reCAPTCHA v3 never shows anything.
   Nothing here runs when verification is switched off. */
(function () {
  const cfg = window.DSS_CAPTCHA || { enabled: false };
  window.dssCaptcha = { enabled: !!cfg.enabled, getToken: () => Promise.resolve("") };
  if (!cfg.enabled) return;

  let scriptPromise = null;

  function loadScript() {
    if (scriptPromise) return scriptPromise;
    scriptPromise = new Promise((resolve, reject) => {
      const tag = document.createElement("script");
      tag.async = true;
      tag.defer = true;
      if (cfg.provider === "recaptcha") {
        tag.src = "https://www.google.com/recaptcha/api.js?render=" + encodeURIComponent(cfg.siteKey);
        tag.onload = () => window.grecaptcha.ready(resolve);
      } else {
        tag.src = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";
        tag.onload = () => resolve();
      }
      tag.onerror = () => { scriptPromise = null; reject(new Error("verification script did not load")); };
      document.head.appendChild(tag);
    });
    return scriptPromise;
  }

  function turnstileToken(action) {
    return new Promise((resolve, reject) => {
      const holder = document.createElement("div");
      holder.className = "dss-captcha-holder";
      document.body.appendChild(holder);
      let widgetId = null;
      let settled = false;
      function finish(fn, value) {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        setTimeout(() => {
          try { if (widgetId !== null) window.turnstile.remove(widgetId); } catch (_e) { /* already gone */ }
          holder.remove();
        }, 0);
        fn(value);
      }
      const timer = setTimeout(() => finish(reject, new Error("verification timed out")), 120000);
      widgetId = window.turnstile.render(holder, {
        sitekey: cfg.siteKey,
        action: action,
        appearance: "interaction-only",
        execution: "execute",
        callback: (token) => finish(resolve, token),
        "error-callback": () => { finish(reject, new Error("verification failed")); return true; },
        "expired-callback": () => finish(reject, new Error("verification expired")),
      });
      window.turnstile.execute(holder);
    });
  }

  function getToken(action) {
    const clean = String(action || "submit").replace(/[^A-Za-z0-9_-]/g, "_").slice(0, 32);
    return loadScript().then(() => {
      if (cfg.provider === "recaptcha") return window.grecaptcha.execute(cfg.siteKey, { action: clean });
      return turnstileToken(clean);
    });
  }
  window.dssCaptcha.getToken = getToken;

  // Start loading early on pages that will need it, so the first submit
  // does not wait for the download.
  if (document.querySelector("form[data-captcha]")) loadScript().catch(() => {});

  function showFormError(form, message) {
    let box = form.querySelector("[data-captcha-error]");
    if (!box) {
      box = document.createElement("div");
      box.className = "alert alert-danger small mt-2";
      box.setAttribute("data-captcha-error", "");
      box.setAttribute("role", "alert");
      form.appendChild(box);
    }
    box.textContent = message;
  }

  // 1. Forms
  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.dataset.captcha) return;
    if (form.dataset.captchaReady === "1") { form.dataset.captchaReady = ""; return; }
    event.preventDefault();
    const submitter = event.submitter || null;
    if (submitter) submitter.disabled = true;
    getToken(form.dataset.captcha)
      .then((token) => {
        let field = form.querySelector('input[name="captcha_token"]');
        if (!field) {
          field = document.createElement("input");
          field.type = "hidden";
          field.name = "captcha_token";
          form.appendChild(field);
        }
        field.value = token;
        form.dataset.captchaReady = "1";
        if (submitter) submitter.disabled = false;
        if (typeof form.requestSubmit === "function") form.requestSubmit(submitter || undefined);
        else { if (window.dssShowLoader) window.dssShowLoader(); form.submit(); }
      })
      .catch(() => {
        if (submitter) submitter.disabled = false;
        if (window.dssHideLoader) window.dssHideLoader();
        showFormError(form, "The quick human check could not finish. Check your connection, then try again.");
      });
  }, false);

  // 2. fetch() calls to protected routes
  const originalFetch = window.fetch ? window.fetch.bind(window) : null;
  if (!originalFetch) return;

  function sameOrigin(input) {
    try {
      const url = new URL(typeof input === "string" ? input : input.url, window.location.href);
      return url.origin === window.location.origin;
    } catch (_e) {
      return false;
    }
  }

  let passPromise = null;
  function obtainPass(action) {
    if (passPromise) return passPromise;
    const meta = document.querySelector('meta[name="csrf-token"]');
    passPromise = getToken(action)
      .then((token) => originalFetch("/api/captcha/verify", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRFToken": meta ? meta.content : "" },
        body: JSON.stringify({ token: token, action: action }),
      }))
      .then((response) => response.ok)
      .catch(() => false)
      .finally(() => { setTimeout(() => { passPromise = null; }, 0); });
    return passPromise;
  }

  window.fetch = function (input, init) {
    return originalFetch(input, init).then((response) => {
      if (response.status !== 403 || response.headers.get("X-Captcha-Required") !== "1" || !sameOrigin(input)) {
        return response;
      }
      const action = response.headers.get("X-Captcha-Action") || "analysis";
      return obtainPass(action).then((ok) => (ok ? originalFetch(input, init) : response));
    });
  };
})();
