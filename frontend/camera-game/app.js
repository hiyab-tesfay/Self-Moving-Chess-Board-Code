"use strict";
let state = null;
let sending = false;
const byId = id => document.getElementById(id);
const titles = {setup:"Confirm the starting position",human:"Your turn · White",thinking:"Stockfish is thinking",remove:"Remove the captured piece",computer:"Black's move",choose:"Choose your move",finished:"Game over",stopped:"Game stopped"};
async function send(action, extra = {}) {
  if (!state || sending && action !== "stop") return;
  sending = true;
  byId("confirm").disabled = true;
  try {
    const response = await fetch(`/api/${action}`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({revision:state.revision,...extra})});
    if (!response.ok) byId("connection").textContent = "The game changed. Refreshing the current position…";
  } catch (_) { byId("connection").textContent = "Connection lost. Reconnect to the Pi before confirming."; }
  finally { sending = false; await refresh(); }
}
function render(next) {
  state = next;
  byId("phase").textContent = next.busy ? "Checking or moving…" : titles[next.phase] || "Set up on the Pi touchscreen";
  byId("message").textContent = next.message || "Home the gantry at e2, calibrate the camera, and start a game on the touchscreen.";
  byId("confirm").textContent = next.confirm_label || "Confirm move";
  byId("confirm").disabled = !next.can_confirm || next.pending || sending;
  byId("connection").textContent = "Connected to the same game as the Pi touchscreen.";
  byId("moves").textContent = next.moves?.join("  ") || "No moves yet.";
  byId("choices").replaceChildren(...(next.choices || []).map(choice => {
    const button = document.createElement("button"); button.textContent = choice.label;
    button.disabled = next.busy || next.pending || sending;
    button.addEventListener("click", () => send("choice", {uci:choice.uci})); return button;
  }));
  const pieces = next.pieces || {};
  const squares = [];
  for (let rank=8; rank>=1; rank--) for (let file=0; file<8; file++) {
    const name = "abcdefgh"[file]+rank, piece=pieces[name], square=document.createElement("div");
    square.className = "square" + ((file+rank)%2 ? " dark" : "") + (piece?.color === "white" ? " white" : "");
    square.textContent = piece?.symbol || ""; square.setAttribute("aria-label",name+(piece ? " "+piece.name : " empty"));
    const label=document.createElement("small"); label.textContent=name; square.append(label); squares.push(square);
  }
  byId("board").replaceChildren(...squares);
}
let refreshing = false;
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const response=await fetch("/api/state",{cache:"no-store"}); if(!response.ok) throw new Error();
    const next=await response.json();
    if(next.mode !== "camera-game") {
      state=null; byId("confirm").disabled=true;
      byId("phase").textContent="Camera game is closed";
      byId("connection").textContent="Open Camera Chess on the Pi touchscreen. Refresh this page to use the manual dashboard instead.";
    } else render(next);
  }
  catch (_) { byId("confirm").disabled=true; byId("connection").textContent="Pi connection unavailable. The game stays on the Pi."; }
  finally { refreshing=false; }
}
byId("confirm").addEventListener("click",()=>send("confirm"));
byId("stop").addEventListener("click",()=>send("stop"));
refresh(); setInterval(refresh,700);
