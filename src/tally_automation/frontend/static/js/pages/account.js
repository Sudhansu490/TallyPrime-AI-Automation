// Account: who is signed in, and changing one's own password.

import { api } from "../api.js";
import { clear, el, formatDateTime, showError, showMessage } from "../ui.js";

const $ = (id) => document.getElementById(id);

async function loadMe() {
  const me = await api.get("/auth/me");
  const list = $("me");
  clear(list);
  for (const [label, value] of [["Name", me.display_name], ["Username", me.username], ["Role", me.role_label],
    ["Previous login", formatDateTime(me.last_login_at)]]) {
    list.append(el("dt", { text: label }), el("dd", { text: value }));
  }
}

$("password-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const current = $("current-password").value;
  const next = $("new-password").value;
  if (next !== $("repeat-password").value) {
    showMessage($("password-message"), "error", "The two new passwords differ.");
    return;
  }
  $("password-button").disabled = true;
  showMessage($("password-message"), "loading", "Changing…");
  try {
    await api.post("/auth/password", { current_password: current, new_password: next });
    // The server started a new session with a new security token: reload to pick it up.
    window.location.reload();
  } catch (error) {
    showError($("password-message"), error);
    $("password-button").disabled = false;
  } finally {
    for (const id of ["current-password", "new-password", "repeat-password"]) $(id).value = "";
  }
});

loadMe().catch((error) => showError($("password-message"), error));
