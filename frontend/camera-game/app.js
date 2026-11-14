"use strict";
let state = null;
let sending = false;
let editingRating = false;
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
  if (state && state.revision === next.revision && state.pending === next.pending) return;
  const previousPhase = state?.phase;
  state = next;
  const difficulty = next.difficulty;
  byId("difficulty").hidden = !difficulty;
  if (difficulty) {
    const slider = byId("rating");
    slider.min = difficulty.min; slider.max = difficulty.max;
    slider.disabled = !difficulty.enabled || next.pending || sending;
    if (!editingRating) {
      slider.value = difficulty.rating;
      byId("rating-value").textContent = difficulty.rating;
    }
  }
  byId("phase").textContent = next.busy ? "Checking or moving…" : next.phase === "stopped" ? titles.stopped : next.heading?.title || titles[next.phase] || "Set up your game";
  byId("detail").textContent = next.heading?.detail || "";
  byId("message").textContent = next.message || "Home the gantry at e7, then start your game here.";
  byId("confirm").textContent = next.confirm_label || "Confirm move";
  byId("confirm").hidden = !["setup", "human", "remove", "computer"].includes(next.phase);
  byId("confirm").disabled = !next.can_confirm || next.pending || sending;
  byId("connection").textContent = "Connected to the Pi. All setup and game controls are available here.";
  byId("controls").replaceChildren(...(next.controls || []).filter(control => next.phase !== "gantry" || control.id === "gantry_back").map(control => {
    const button = document.createElement("button"); button.textContent = control.label;
    button.disabled = next.busy || next.pending || sending;
    button.addEventListener("click", () => send("control", {value:control.id})); return button;
  }));
  byId("manual").hidden = next.phase !== "gantry";
  if (next.phase === "gantry" && previousPhase !== "gantry") byId("manual").scrollIntoView({block:"start"});
  const manualReady = next.manual_enabled && !next.pending && !sending;
  byId("manual").querySelectorAll("button, input, select").forEach(input => {
    input.disabled = !manualReady || input.hasAttribute("data-needs-home") && !next.gantry?.homed;
  });
  const st = next.gantry;
  byId("position").textContent = st ? `X ${st.x?.toFixed(2) ?? "—"} mm · Y ${st.y?.toFixed(2) ?? "—"} mm · ${st.state || "—"} · ${st.homed ? "Homed" : "Set home required"} · Arm ${st.servo || "—"} · Magnet ${st.magnet ? "ON" : "off"}` : "Connect to read position.";
  if (st?.response) byId("command-response").textContent = st.response;
  if (next.manual_settings && !document.activeElement.closest("#speed-form")) {
    byId("travel-speed").value = next.manual_settings.travel_speed_mm_s;
    byId("carry-speed").value = next.manual_settings.carry_speed_mm_s;
    byId("accel").value = next.manual_settings.accel_mm_s2;
  }
  const photo = next.photo;
  byId("photo-panel").hidden = !photo;
  byId("board").hidden = !!photo;
  if (photo) {
    if (byId("photo").getAttribute("src") !== photo.url) byId("photo").src = photo.url;
    byId("photo-help").textContent = photo.clickable ? "Click the next board corner shown in the instructions. Exclude the wooden border." : "Check the camera photo and follow the instructions.";
    byId("photo").classList.toggle("selecting", photo.clickable && !next.pending && !next.busy);
    byId("photo-points").replaceChildren(...(photo.points || []).map(([x,y], index) => {
      const dot = document.createElement("span"); dot.style.left = `${100*x}%`; dot.style.top = `${100*y}%`;
      dot.textContent = index+1; return dot;
    }));
  }
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
    square.dataset.square = name;
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
      state=null; byId("rating").disabled=true; byId("confirm").disabled=true;
      byId("phase").textContent="Camera game is closed";
      byId("connection").textContent="The integrated chess service is unavailable. Reconnect to the Pi.";
    } else render(next);
  }
  catch (_) { state=null; byId("rating").disabled=true; byId("confirm").disabled=true; byId("connection").textContent="Pi connection unavailable. The game stays on the Pi."; }
  finally { refreshing=false; }
}
byId("confirm").addEventListener("click",()=>send("confirm"));
byId("stop").addEventListener("click",()=>send("stop"));
const manual = value => send("manual", {value});
document.querySelectorAll("[data-op]").forEach(button => button.addEventListener("click", () => manual({op:button.dataset.op})));
document.querySelectorAll("[data-jog]").forEach(button => button.addEventListener("click", () => {
  const [dx,dy] = button.dataset.jog.split(",").map(Number), selected = byId("jog-step").value;
  manual({op:"jog", dx,dy,step:selected === "square" ? selected : Number(selected)});
}));
byId("goto-form").addEventListener("submit", event => {event.preventDefault(); manual({op:"goto", place:byId("goto-place").value.trim().toLowerCase()});});
byId("carry-form").addEventListener("submit", event => {event.preventDefault(); manual({op:event.submitter.name, uci:byId("manual-uci").value.trim().toLowerCase()});});
byId("raw-form").addEventListener("submit", event => {event.preventDefault(); manual({op:"raw", line:byId("raw-command").value.trim()});});
byId("speed-form").addEventListener("submit", event => {
  event.preventDefault(); manual({op:"settings", travel_speed_mm_s:Number(byId("travel-speed").value), carry_speed_mm_s:Number(byId("carry-speed").value), accel_mm_s2:Number(byId("accel").value)});
});
byId("board").addEventListener("click", event => {
  const square = event.target.closest("[data-square]");
  if (square && state?.phase === "gantry" && state.manual_enabled && state.gantry?.homed && !state.pending) manual({op:"goto", place:square.dataset.square});
});
byId("rating").addEventListener("input", event => {
  editingRating = true; byId("rating-value").textContent = event.target.value;
});
byId("rating").addEventListener("change", event => {
  editingRating = false; send("rating", {rating:Number(event.target.value)});
});
byId("photo").addEventListener("click", event => {
  if (!state?.photo?.clickable || state.busy || state.pending) return;
  const rect = event.target.getBoundingClientRect();
  const x = (event.clientX-rect.left)/rect.width, y = (event.clientY-rect.top)/rect.height;
  if (x >= 0 && x < 1 && y >= 0 && y < 1) send("corner", {value:[x,y]});
});
refresh(); setInterval(refresh,700);
