(() => {
  "use strict";

  const recovery = window.location.pathname === "/recover-access";
  const expectedKey = recovery ? "rt" : "t";
  const params = new URLSearchParams(window.location.hash.slice(1));
  const token = params.get(expectedKey) || "";

  window.history.replaceState(null, "", window.location.pathname);

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
