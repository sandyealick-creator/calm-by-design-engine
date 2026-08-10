(() => {
  "use strict";

  const recovery = window.location.pathname === "/recover-access";
  const expectedKey = recovery ? "rt" : "t";
  const fragment = window.location.hash.slice(1);

  window.history.replaceState(null, "", window.location.pathname);

  let token = "";
  try {
    const params = new URLSearchParams(fragment);
    const keys = Array.from(params.keys());
    const values = params.getAll(expectedKey);
    if (keys.length === 1 && keys[0] === expectedKey && values.length === 1) {
      token = values[0];
    }
  } catch (_error) {
    token = "";
  }

  if (!token) {
    window.location.replace("/link-invalid");
    return;
  }

  fetch(window.location.pathname, {
    method: "POST",
    credentials: "same-origin",
    headers: {"Content-Type": "application/x-www-form-urlencoded"},
    body: new URLSearchParams({token}).toString(),
    redirect: "error"
  }).then((response) => {
    window.location.replace(response.ok ? "/checkin" : "/link-invalid");
  }).catch(() => {
    window.location.replace("/link-invalid");
  });
})();
