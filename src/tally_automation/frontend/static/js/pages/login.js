// Login: sends the credentials once; the server sets an HttpOnly session cookie.
// Nothing is stored in browser storage.

import { api } from "../api.js";
import { showError, showMessage } from "../ui.js";

const $ = (id) => document.getElementById(id);

function nextPath() {
  const next = new URLSearchParams(window.location.search).get("next") || "/";
  // Same-site relative paths only (the server checks this too).
  return next.charAt(0) === "/" && next.charAt(1) !== "/" && !next.includes("\\") ? next : "/";
}

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("login-button");
  button.disabled = true;
  showMessage($("login-message"), "loading", "Checking…");
  try {
    await api.post("/auth/login", { username: $("username").value.trim(), password: $("password").value });
    window.location.assign(nextPath());
  } catch (error) {
    $("password").value = "";
    showError($("login-message"), error);
    button.disabled = false;
    $("password").focus();
  }
});
