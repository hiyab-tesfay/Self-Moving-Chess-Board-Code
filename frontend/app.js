/* app.js — board UI, clocks, move list, game flow.
 *
 * Rules come from chess.js (client side, for instant hints and highlights).
 * When the page is served by frontend/server.py, computer games go through the
 * chess_game.py backend (python-chess + Stockfish) via the /api/* endpoints.
 * Without a backend (plain static hosting) the computer falls back to a casual
 * random mover so the UI still works.
 */
(() => {
  'use strict';

  const $ = (s, el) => (el || document).querySelector(s);
  const $$ = (s, el) => Array.from((el || document).querySelectorAll(s));
  const { sqName, fileOf, rankOf, colorOf, typeOf, opp, FLAGS } = Chess;
  const sq = (f, r) => r * 8 + f;

  const TIME_CONTROLS = [
    { id: '1+0', label: '1 min', group: 'Bullet', base: 60, inc: 0 },
    { id: '1+1', label: '1 | 1', group: 'Bullet', base: 60, inc: 1 },
    { id: '2+1', label: '2 | 1', group: 'Bullet', base: 120, inc: 1 },
    { id: '3+0', label: '3 min', group: 'Blitz', base: 180, inc: 0 },
    { id: '3+2', label: '3 | 2', group: 'Blitz', base: 180, inc: 2 },
    { id: '5+0', label: '5 min', group: 'Blitz', base: 300, inc: 0 },
    { id: '10+0', label: '10 min', group: 'Rapid', base: 600, inc: 0 },
    { id: '15+10', label: '15 | 10', group: 'Rapid', base: 900, inc: 10 },
    { id: '30+0', label: '30 min', group: 'Rapid', base: 1800, inc: 0 },
    { id: 'inf', label: 'Unlimited', group: 'Casual', base: 0, inc: 0 },
  ];
  const GROUP_ICON = { Bullet: '🚀', Blitz: '⚡', Rapid: '⏱', Casual: '☕' };

  // ----------------------------------------------------------------- backend (chess_game.py via server.py)

  const Backend = {
    available: false,
    engine: false,
    engineError: null,
    levels: [],
    async probe() {
      try {
        const ctrl = new AbortController();
        const timer = setTimeout(() => ctrl.abort(), 2000);
        const r = await fetch('api/status', { signal: ctrl.signal, cache: 'no-store' });
        clearTimeout(timer);
        if (!r.ok) throw new Error('status ' + r.status);
        const s = await r.json();
        this.available = true;
        this.engine = !!s.engine;
        this.engineError = s.engineError || null;
        this.levels = Array.isArray(s.levels) && s.levels.length ? s.levels : [];
      } catch (e) {
        this.available = false;
      }
      return this.available;
    },
    async call(path, body) {
      const r = await fetch(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {}),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) {
        const err = new Error(data.error || ('Server error ' + r.status));
        err.data = data;
        throw err;
      }
      return data;
    },
    newGame(level) { return this.call('api/new', { level }); },
    move(from, to, promotion) { return this.call('api/move', { from, to, promotion: promotion || null }); },
    undo() { return this.call('api/undo', {}); },
  };

  // ----------------------------------------------------------------- state

  const state = {
    game: new Chess(),
    status: 'idle',          // idle | playing | over
    mode: 'computer',        // engine (backend Stockfish) | computer (local casual bot) | friend
    playerColor: 'w',
    colorPref: 'random',
    level: 'beginner',
    tc: TIME_CONTROLS[6],
    clocks: { w: 0, b: 0 },
    running: null,
    lastTick: 0,
    orientation: 'w',
    selected: null,
    legal: [],
    viewPly: null,           // null = live position
    snapshots: [],
    result: null,
    gameId: 0,
    botTimer: null,
    thinking: false,
    pendingPromo: null,
    drag: null,
    sound: true,
    pieceStyle: 'duen',
    hoverSq: null,
  };

  const el = {
    board: $('#board'), squares: $('#squares'), pieces: $('#pieces'), promo: $('#promo'), toast: $('#toast'),
    ranks: $('#ranks'), files: $('#files'),
    top: $('#player-top'), bottom: $('#player-bottom'),
    panelNew: $('#panel-new'), panelGame: $('#panel-game'),
    tcButton: $('#tc-button'), tcLabel: $('#tc-label'), tcMenu: $('#tc-menu'),
    levelRow: $('#level-row'), levelPick: $('#level-pick'), backendStatus: $('#backend-status'),
    playBtn: $('#play-btn'), playFriend: $('#play-friend'), playBot: $('#play-bot'),
    playBotTitle: $('#play-bot-title'), playBotSub: $('#play-bot-sub'),
    colorRow: $('#color-row'), colorPick: $('#color-pick'),
    gameTitle: $('#game-title'), moves: $('#moves'),
    confirm: $('#confirm'), confirmText: $('#confirm-text'), confirmYes: $('#confirm-yes'), confirmNo: $('#confirm-no'),
    btnDraw: $('#btn-draw'), btnResign: $('#btn-resign'), resignLabel: $('#resign-label'), btnTakeback: $('#btn-takeback'),
    navFirst: $('#nav-first'), navPrev: $('#nav-prev'), navNext: $('#nav-next'), navLast: $('#nav-last'), btnFlip: $('#btn-flip'),
    afterGame: $('#after-game'), btnRematch: $('#btn-rematch'), btnNewGame: $('#btn-newgame'),
    btnPgn: $('#btn-pgn'), btnFen: $('#btn-fen'),
    modal: $('#modal'), modalClose: $('#modal-close'), modalHeader: $('#modal-header'), modalTitle: $('#modal-title'),
    modalSub: $('#modal-sub'), modalScore: $('#modal-score'), modalNameW: $('#modal-name-w'), modalNameB: $('#modal-name-b'),
    modalAvW: $('#modal-av-w'), modalAvB: $('#modal-av-b'), modalRematch: $('#modal-rematch'), modalNew: $('#modal-new'),
    soundToggle: $('#sound-toggle'),
  };

  // ----------------------------------------------------------------- sound (synthesised, no assets)

  const Sound = {
    ctx: null,
    ensure() {
      if (!this.ctx) {
        const AC = window.AudioContext || window.webkitAudioContext;
        if (AC) this.ctx = new AC();
      }
      if (this.ctx && this.ctx.state === 'suspended') this.ctx.resume();
      return this.ctx;
    },
    tone(freq, dur, type, gain, when) {
      const ctx = this.ctx;
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.type = type || 'sine';
      o.frequency.value = freq;
      g.gain.setValueAtTime(0.0001, ctx.currentTime + when);
      g.gain.exponentialRampToValueAtTime(gain, ctx.currentTime + when + 0.005);
      g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + when + dur);
      o.connect(g).connect(ctx.destination);
      o.start(ctx.currentTime + when);
      o.stop(ctx.currentTime + when + dur + 0.02);
    },
    noise(dur, gain, when) {
      const ctx = this.ctx;
      const buf = ctx.createBuffer(1, Math.floor(ctx.sampleRate * dur), ctx.sampleRate);
      const d = buf.getChannelData(0);
      for (let i = 0; i < d.length; i++) d[i] = (Math.random() * 2 - 1) * (1 - i / d.length);
      const src = ctx.createBufferSource(); src.buffer = buf;
      const f = ctx.createBiquadFilter(); f.type = 'lowpass'; f.frequency.value = 1800;
      const g = ctx.createGain(); g.gain.value = gain;
      src.connect(f).connect(g).connect(ctx.destination);
      src.start(ctx.currentTime + when);
    },
    play(kind) {
      if (!state.sound || !this.ensure()) return;
      try {
        switch (kind) {
          case 'move': this.noise(0.04, 0.5, 0); this.tone(220, 0.08, 'sine', 0.25, 0); break;
          case 'capture': this.noise(0.06, 0.8, 0); this.tone(160, 0.1, 'triangle', 0.35, 0); this.tone(110, 0.12, 'sine', 0.3, 0.03); break;
          case 'castle': this.noise(0.04, 0.5, 0); this.tone(220, 0.08, 'sine', 0.25, 0); this.noise(0.04, 0.4, 0.1); this.tone(200, 0.08, 'sine', 0.2, 0.1); break;
          case 'check': this.tone(880, 0.12, 'sine', 0.25, 0); this.tone(1320, 0.14, 'sine', 0.15, 0.06); break;
          case 'illegal': this.tone(120, 0.12, 'square', 0.08, 0); break;
          case 'start': this.tone(523, 0.1, 'sine', 0.2, 0); this.tone(659, 0.12, 'sine', 0.2, 0.09); break;
          case 'end': this.tone(523, 0.14, 'sine', 0.22, 0); this.tone(392, 0.14, 'sine', 0.22, 0.14); this.tone(330, 0.25, 'sine', 0.22, 0.28); break;
          case 'win': this.tone(523, 0.12, 'sine', 0.22, 0); this.tone(659, 0.12, 'sine', 0.22, 0.12); this.tone(784, 0.12, 'sine', 0.22, 0.24); this.tone(1047, 0.3, 'sine', 0.25, 0.36); break;
        }
      } catch (e) { /* ignore audio errors */ }
    },
  };

  // ----------------------------------------------------------------- helpers

  const toXY = (i) => {
    const f = fileOf(i), r = rankOf(i);
    return state.orientation === 'w' ? [f, 7 - r] : [7 - f, r];
  };
  const fromXY = (x, y) => (state.orientation === 'w' ? sq(x, 7 - y) : sq(7 - x, y));

  function fmtClock(ms) {
    ms = Math.max(0, ms);
    const total = Math.ceil(ms / 1000);
    if (ms < 10000) {
      const s = Math.floor(ms / 1000), t = Math.floor((ms % 1000) / 100);
      return `0:0${s}.${t}`;
    }
    const m = Math.floor(total / 60), s = total % 60;
    return (m >= 60 ? `${Math.floor(m / 60)}:${String(m % 60).padStart(2, '0')}` : m) + ':' + String(s).padStart(2, '0');
  }

  function toast(msg, ms) {
    el.toast.textContent = msg;
    el.toast.classList.remove('hidden');
    clearTimeout(toast.t);
    toast.t = setTimeout(() => el.toast.classList.add('hidden'), ms || 2200);
  }

  function levelInfo(id) {
    return Backend.levels.find((l) => l.id === id) || { id, label: id.charAt(0).toUpperCase() + id.slice(1), description: '' };
  }

  function playerNames() {
    if (state.mode === 'engine') {
      const you = { name: 'You', cls: 'you', initial: 'Y', sub: '' };
      const bot = { name: 'Stockfish', cls: 'bot', initial: '🤖', sub: levelInfo(state.level).label };
      return state.playerColor === 'w' ? { w: you, b: bot } : { w: bot, b: you };
    }
    if (state.mode === 'computer') {
      const you = { name: 'You', cls: 'you', initial: 'Y', sub: '' };
      const bot = { name: 'Computer', cls: 'bot', initial: '🤖', sub: 'casual' };
      return state.playerColor === 'w' ? { w: you, b: bot } : { w: bot, b: you };
    }
    return { w: { name: 'White', cls: 'white', initial: '♔', sub: '' }, b: { name: 'Black', cls: 'black', initial: '♚', sub: '' } };
  }

  function opponentLabel() {
    if (state.mode === 'engine') return `vs Stockfish (${levelInfo(state.level).label})`;
    if (state.mode === 'computer') return 'vs Computer';
    return 'Pass & Play';
  }

  function tcTitle(tc) {
    return tc.base ? `${tc.label} • ${tc.group}` : 'Unlimited';
  }

  function save() {
    try {
      localStorage.setItem('duen.chess.prefs', JSON.stringify({ tc: state.tc.id, color: state.colorPref, sound: state.sound, level: state.level, pieceStyle: state.pieceStyle }));
    } catch (e) { /* ignore */ }
  }
  function restore() {
    try {
      const p = JSON.parse(localStorage.getItem('duen.chess.prefs') || '{}');
      if (p.tc) state.tc = TIME_CONTROLS.find((t) => t.id === p.tc) || state.tc;
      if (p.color) state.colorPref = p.color;
      if (p.level) state.level = p.level;
      if (p.pieceStyle === 'regular') state.pieceStyle = 'regular';
      if (typeof p.sound === 'boolean') state.sound = p.sound;
    } catch (e) { /* ignore */ }
  }

  function pieceAsset(piece) {
    return `assets/${state.pieceStyle === 'regular' ? 'regular' : 'pieces'}/${piece}.svg`;
  }

  function applyPieceStyle() {
    for (const color of ['w', 'b']) {
      for (const type of ['K', 'Q', 'R', 'B', 'N', 'P']) {
        document.documentElement.style.setProperty(`--piece-${color}${type}`, `url("${pieceAsset(color + type)}")`);
      }
    }
    $$('.piece-style-options button').forEach((button) => {
      button.setAttribute('aria-pressed', String(button.dataset.pieceStyle === state.pieceStyle));
    });
    $$('#color-pick img').forEach((image) => {
      image.src = pieceAsset(image.closest('button').dataset.color + 'K');
    });
    renderPlayers();
    if (state.pendingPromo) {
      const { from, to, cands, opts } = state.pendingPromo;
      showPromotion(from, to, cands, opts);
    }
  }

  $$('.piece-style-options button').forEach((button) => {
    button.addEventListener('click', () => {
      state.pieceStyle = button.dataset.pieceStyle;
      save();
      applyPieceStyle();
    });
  });

  // ----------------------------------------------------------------- board rendering

  function buildSquares() {
    el.squares.innerHTML = '';
    for (let i = 0; i < 64; i++) {
      const d = document.createElement('div');
      d.className = 'sq';
      el.squares.appendChild(d);
    }
  }

  function renderFrameLabels() {
    const ranks = state.orientation === 'w' ? [8, 7, 6, 5, 4, 3, 2, 1] : [1, 2, 3, 4, 5, 6, 7, 8];
    const files = state.orientation === 'w' ? 'abcdefgh' : 'hgfedcba';
    el.ranks.innerHTML = ranks.map((r) => `<span>${r}</span>`).join('');
    el.files.innerHTML = files.split('').map((f) => `<span>${f}</span>`).join('');
  }

  function currentSnapshot() {
    const snaps = state.snapshots;
    if (state.viewPly === null) return snaps[snaps.length - 1];
    return snaps[Math.min(state.viewPly, snaps.length - 1)];
  }

  function pushSnapshot(rec) {
    const g = state.game;
    state.snapshots.push({
      board: g.board.slice(),
      lastMove: rec ? { from: rec.move.from, to: rec.move.to } : null,
      check: g.inCheck(),
      kingSq: g.kings[g.turn],
      turn: g.turn,
    });
  }

  /** Repaint squares and pieces. `anim` maps destination square -> origin square to slide from. */
  function renderBoard(anim) {
    const snap = currentSnapshot();
    const live = state.viewPly === null;
    const cells = el.squares.children;
    const legalTargets = new Set(live ? state.legal.map((m) => m.to) : []);
    const canMove = live && canInteract();

    for (let i = 0; i < 64; i++) {
      const x = i % 8, y = Math.floor(i / 8);
      const sqi = fromXY(x, y);
      const f = fileOf(sqi), r = rankOf(sqi);
      const c = cells[i];
      let cls = 'sq ' + (((f + r) & 1) ? 'light' : 'dark');
      if (snap.lastMove && (snap.lastMove.from === sqi || snap.lastMove.to === sqi)) cls += ' last';
      if (live && state.selected === sqi) cls += ' selected';
      if (snap.check && snap.kingSq === sqi) cls += ' check';
      if (legalTargets.has(sqi)) cls += snap.board[sqi] || isEpTarget(sqi) ? ' capture' : ' hint';
      if (state.hoverSq === sqi && state.drag) cls += ' hover';
      const p = snap.board[sqi];
      if (canMove && p && colorOf(p) === state.game.turn) cls += ' own';
      c.className = cls;
      c.dataset.sq = sqi;
    }

    el.pieces.innerHTML = '';
    const pending = [];
    for (let sqi = 0; sqi < 64; sqi++) {
      const p = snap.board[sqi];
      if (!p) continue;
      const d = document.createElement('div');
      d.className = `piece ${colorOf(p)}${typeOf(p).toUpperCase()} noanim`;
      d.dataset.sq = sqi;
      const from = anim && anim[sqi] !== undefined ? anim[sqi] : sqi;
      const [x, y] = toXY(from);
      d.style.transform = `translate(${x * 100}%, ${y * 100}%)`;
      el.pieces.appendChild(d);
      if (from !== sqi) pending.push([d, sqi]);
    }
    if (pending.length) {
      void el.pieces.offsetWidth; // force layout, then animate to destination
      for (const [d, sqi] of pending) {
        d.classList.remove('noanim');
        const [x, y] = toXY(sqi);
        d.style.transform = `translate(${x * 100}%, ${y * 100}%)`;
      }
    }
    el.board.classList.toggle('grab', canMove);
  }

  function isEpTarget(sqi) {
    return state.legal.some((m) => m.to === sqi && (m.flags & FLAGS.EP));
  }

  // ----------------------------------------------------------------- side panels

  function renderPlayers() {
    const names = playerNames();
    const bottomColor = state.orientation;
    const topColor = opp(bottomColor);
    const captured = state.game.capturedPieces();
    const balance = state.game.materialBalance();
    for (const [bar, color] of [[el.top, topColor], [el.bottom, bottomColor]]) {
      const n = names[color];
      const av = $('.avatar', bar);
      av.className = 'avatar ' + n.cls;
      av.textContent = n.initial;
      let name = n.name + (n.sub ? `<small>(${n.sub})</small>` : '');
      if (state.thinking && n.cls === 'bot' && state.status === 'playing') name += '<span class="thinking">thinking…</span>';
      $('.player-name', bar).innerHTML = name;
      const cap = $('.captured', bar);
      let html = '';
      const groups = {};
      for (const t of captured[color]) (groups[t] = groups[t] || []).push(t);
      for (const t of ['p', 'n', 'b', 'r', 'q']) {
        if (!groups[t]) continue;
        html += '<span class="grp">' + groups[t].map(() => `<img src="${pieceAsset(opp(color) + t.toUpperCase())}" alt="">`).join('') + '</span>';
      }
      const adv = color === 'w' ? balance : -balance;
      if (adv > 0) html += `<span class="adv">+${adv}</span>`;
      cap.innerHTML = html;
    }
    renderClocks();
  }

  function renderClocks() {
    const bottomColor = state.orientation;
    for (const [bar, color] of [[el.top, opp(bottomColor)], [el.bottom, bottomColor]]) {
      const c = $('.clock', bar);
      if (!state.tc.base) { c.className = 'clock none'; continue; }
      const ms = state.status === 'idle' ? state.tc.base * 1000 : state.clocks[color];
      c.textContent = fmtClock(ms);
      c.className = 'clock' +
        (state.status === 'playing' && state.running === color ? ' active' : '') +
        (ms < 20000 && state.status !== 'idle' ? ' low' : '');
    }
  }

  function renderMoves() {
    const h = state.game.history;
    if (!h.length) {
      el.moves.innerHTML = '<div class="empty">Make a move to start the game</div>';
      return;
    }
    let html = '';
    const curPly = state.viewPly === null ? h.length : state.viewPly;
    for (let i = 0; i < h.length; i += 2) {
      const w = h[i], b = h[i + 1];
      html += `<div class="mrow"><span class="num">${i / 2 + 1}.</span>` +
        `<span class="mv${curPly === i + 1 ? ' cur' : ''}" data-ply="${i + 1}">${w.san}</span>` +
        (b ? `<span class="mv${curPly === i + 2 ? ' cur' : ''}" data-ply="${i + 2}">${b.san}</span>` : '<span></span>') +
        '</div>';
    }
    if (state.status === 'over' && state.result) {
      const r = state.result;
      const score = r.winner === 'w' ? '1-0' : r.winner === 'b' ? '0-1' : r.reason === 'aborted' ? '' : '½-½';
      html += `<div class="result-row">${score}<small>${resultText(r)}</small></div>`;
    }
    el.moves.innerHTML = html;
    const cur = $('.mv.cur', el.moves);
    if (cur) cur.scrollIntoView({ block: 'nearest' });
  }

  function reasonText(reason) {
    return { checkmate: 'by checkmate', resignation: 'by resignation', timeout: 'on time', agreement: 'by agreement',
      stalemate: 'by stalemate', 'threefold repetition': 'by repetition', '50-move rule': 'by 50-move rule',
      'insufficient material': 'by insufficient material', 'timeout vs insufficient material': 'by timeout vs insufficient material' }[reason] || reason;
  }

  function resultText(r) {
    if (r.reason === 'aborted') return 'Game aborted';
    const names = playerNames();
    const who = r.winner ? names[r.winner].name : null;
    return who ? `${who} won ${reasonText(r.reason)}` : `Draw ${reasonText(r.reason)}`;
  }

  function renderControls() {
    const playing = state.status === 'playing';
    const plies = state.game.history.length;
    el.btnDraw.disabled = !playing || state.thinking;
    el.btnResign.disabled = !playing;
    el.resignLabel.textContent = playing && plies < 2 ? 'Abort' : 'Resign';
    el.btnTakeback.disabled = !playing || plies === 0 || state.thinking;
    const cur = state.viewPly === null ? plies : state.viewPly;
    el.navFirst.disabled = el.navPrev.disabled = cur === 0;
    el.navNext.disabled = el.navLast.disabled = cur >= plies;
    el.afterGame.classList.toggle('hidden', state.status !== 'over');
    el.gameTitle.textContent = `${tcTitle(state.tc)} • ${opponentLabel()}`;
  }

  function renderAll(anim) {
    renderFrameLabels();
    renderBoard(anim);
    renderPlayers();
    renderMoves();
    renderControls();
  }

  function renderNewGamePanel() {
    el.tcLabel.textContent = state.tc.label;
    $$('button', el.colorPick).forEach((b) => b.classList.toggle('on', b.dataset.color === state.colorPref));
    // time control menu
    const groups = [];
    for (const tc of TIME_CONTROLS) {
      let g = groups.find((x) => x.name === tc.group);
      if (!g) groups.push((g = { name: tc.group, items: [] }));
      g.items.push(tc);
    }
    el.tcMenu.innerHTML = groups.map((g) =>
      `<div class="tc-group"><div class="tc-group-name">${GROUP_ICON[g.name] || ''} ${g.name}</div><div class="tc-grid">` +
      g.items.map((tc) => `<button type="button" class="tc-opt${tc.id === state.tc.id ? ' on' : ''}" data-tc="${tc.id}">${tc.label}</button>`).join('') +
      '</div></div>').join('');
    el.soundToggle.classList.toggle('off', !state.sound);

    // backend-driven options
    if (Backend.available) {
      if (!Backend.levels.some((l) => l.id === state.level)) state.level = (Backend.levels[0] || { id: 'beginner' }).id;
      el.levelRow.classList.remove('hidden');
      el.levelPick.innerHTML = Backend.levels.map((l) =>
        `<button type="button" class="${l.id === state.level ? 'on' : ''}" data-level="${l.id}" title="${l.description || ''}">${l.label}<small>${l.description || ''}</small></button>`).join('');
      el.colorRow.classList.add('hidden');
      el.playBotTitle.textContent = 'Play Stockfish';
      el.playBotSub.textContent = 'You play White · chess_game backend';
      if (Backend.engine) {
        el.backendStatus.textContent = 'Connected to the chess_game backend · Stockfish ready';
        el.backendStatus.className = 'backend-status ok';
      } else {
        el.backendStatus.textContent = 'Backend connected, but Stockfish was not found: ' + (Backend.engineError || 'install it or set STOCKFISH_PATH');
        el.backendStatus.className = 'backend-status warn';
      }
    } else {
      el.levelRow.classList.add('hidden');
      el.colorRow.classList.remove('hidden');
      el.playBotTitle.textContent = 'Play Bots';
      el.playBotSub.textContent = 'Casual game vs the computer';
      el.backendStatus.textContent = 'No backend detected · casual bot (run frontend/server.py for Stockfish)';
      el.backendStatus.className = 'backend-status';
    }
  }

  // ----------------------------------------------------------------- game flow

  function resetGameState() {
    clearTimeout(state.botTimer);
    state.gameId++;
    state.game = new Chess();
    state.result = null;
    state.selected = null;
    state.legal = [];
    state.viewPly = null;
    state.pendingPromo = null;
    state.drag = null;
    state.thinking = false;
    state.snapshots = [];
    pushSnapshot(null);
    state.clocks = { w: state.tc.base * 1000, b: state.tc.base * 1000 };
    state.running = null;
    el.promo.classList.add('hidden');
    el.confirm.classList.add('hidden');
    hideModal();
  }

  async function startGame(mode, colorPref) {
    if (mode === 'engine' && !Backend.available) mode = 'computer';
    resetGameState();
    const id = state.gameId;
    state.mode = mode;
    state.status = 'playing';
    const pref = colorPref || state.colorPref;
    state.playerColor = mode === 'engine' ? 'w' : pref === 'random' ? (Math.random() < 0.5 ? 'w' : 'b') : pref;
    state.orientation = mode === 'friend' ? 'w' : state.playerColor;
    el.panelNew.classList.add('hidden');
    el.panelGame.classList.remove('hidden');
    renderAll();

    if (mode === 'engine') {
      try {
        await Backend.newGame(state.level);
      } catch (e) {
        if (id !== state.gameId) return;
        toast('Backend error: ' + e.message + ' — playing the casual bot instead', 4000);
        state.mode = 'computer';
        renderAll();
      }
      if (id !== state.gameId) return;
    }
    Sound.play('start');
    if (state.mode === 'computer' && state.game.turn !== state.playerColor) scheduleBot();
  }

  function canInteract() {
    return state.status === 'playing' && !state.pendingPromo && !state.thinking &&
      (state.mode === 'friend' || state.game.turn === state.playerColor);
  }

  function select(sqi) {
    state.selected = sqi;
    state.legal = state.game.movesFrom(sqi);
    renderBoard();
  }

  function deselect() {
    if (state.selected === null && !state.legal.length) return;
    state.selected = null;
    state.legal = [];
    renderBoard();
  }

  /** Attempt a move from -> to. Returns true if the move was played or a promotion picker opened. */
  function tryMove(from, to, opts) {
    const cands = state.game.movesFrom(from).filter((m) => m.to === to);
    if (!cands.length) return false;
    if (cands[0].promo) {
      showPromotion(from, to, cands, opts);
      return true;
    }
    commitMove(cands[0], opts);
    return true;
  }

  /**
   * Play a move on the local board and update everything.
   * `input` is a move object from chess.js or a UCI string; opts.source is
   * 'human' | 'bot' | 'engine' (engine = already played on the backend).
   */
  function commitMove(input, opts) {
    opts = opts || {};
    const g = state.game;
    const mover = g.turn;
    const rec = g.move(input);
    if (!rec) return null;
    const m = rec.move;
    pushSnapshot(rec);

    if (state.tc.base) {
      if (state.running) tick();
      state.clocks[mover] += state.tc.inc * 1000;
      state.running = g.turn;
      state.lastTick = performance.now();
    }

    state.selected = null;
    state.legal = [];
    state.viewPly = null;
    state.pendingPromo = null;
    el.promo.classList.add('hidden');
    el.confirm.classList.add('hidden');

    let anim = null;
    if (opts.animate !== false) {
      anim = { [m.to]: m.from };
      if (m.flags & FLAGS.CASTLE_K) anim[m.to - 1] = m.to + 1;
      if (m.flags & FLAGS.CASTLE_Q) anim[m.to + 1] = m.to - 2;
    }
    renderAll(anim);

    const over = g.gameOver();
    if (over) {
      endGame(over.winner, over.reason);
      return rec;
    }
    Sound.play(rec.check ? 'check' : rec.captured ? 'capture' : (m.flags & (FLAGS.CASTLE_K | FLAGS.CASTLE_Q)) ? 'castle' : 'move');

    if (g.turn !== state.playerColor) {
      if (state.mode === 'computer') scheduleBot();
      else if (state.mode === 'engine' && opts.source !== 'engine') requestEngineMove(rec);
    }
    return rec;
  }

  /** Send the human move to chess_game.py and play Stockfish's reply. */
  async function requestEngineMove(rec) {
    const id = state.gameId;
    state.thinking = true;
    renderPlayers();
    renderControls();
    try {
      const res = await Backend.move(sqName(rec.move.from), sqName(rec.move.to), rec.move.promo || null);
      if (id !== state.gameId || state.status !== 'playing') return;
      state.thinking = false;
      if (res.computer && res.computer.uci) {
        const played = commitMove(res.computer.uci, { animate: true, source: 'engine' });
        if (!played) throw new Error('Stockfish played ' + res.computer.uci + ', which the board rejected');
      } else {
        renderPlayers();
        renderControls();
      }
      if (res.fen && !sameBoard(res.fen, state.game.fen())) {
        console.warn('Board differs from backend', res.fen, state.game.fen());
        toast('Board is out of sync with the backend — start a new game', 4000);
      }
    } catch (e) {
      if (id !== state.gameId) return;
      state.thinking = false;
      renderPlayers();
      renderControls();
      toast(e.message, 4500);
    }
  }

  function sameBoard(fenA, fenB) {
    const a = fenA.split(' '), b = fenB.split(' ');
    return a[0] === b[0] && a[1] === b[1] && a[2] === b[2];
  }

  /** Offline "computer": a casual random mover that likes captures. No search, no evaluation. */
  function scheduleBot() {
    const id = state.gameId;
    clearTimeout(state.botTimer);
    state.botTimer = setTimeout(() => {
      if (id !== state.gameId || state.status !== 'playing') return;
      const g = state.game;
      const moves = g.legalMoves();
      if (!moves.length) return;
      const caps = moves.filter((m) => m.flags & FLAGS.CAPTURE);
      const pool = caps.length && Math.random() < 0.65 ? caps : moves;
      let m = pool[Math.floor(Math.random() * pool.length)];
      if (m.promo) m = pool.find((x) => x.from === m.from && x.to === m.to && x.promo === 'q') || m;
      commitMove(m, { animate: true, source: 'bot' });
    }, 450 + Math.random() * 800);
  }

  function endGame(winner, reason) {
    state.status = 'over';
    state.running = null;
    state.thinking = false;
    clearTimeout(state.botTimer);
    state.result = { winner, reason };
    state.selected = null;
    state.legal = [];
    state.pendingPromo = null;
    el.promo.classList.add('hidden');
    el.confirm.classList.add('hidden');
    renderAll();
    if (reason !== 'aborted') {
      const youWon = state.mode !== 'friend' && winner === state.playerColor;
      Sound.play(youWon ? 'win' : 'end');
      showModal();
    }
  }

  function showModal() {
    const r = state.result;
    const names = playerNames();
    let title, cls = '';
    if (state.mode !== 'friend') {
      if (r.winner === state.playerColor) title = 'You Won!';
      else if (r.winner) { title = 'You Lost'; cls = 'lose'; }
      else { title = 'Draw'; cls = 'draw'; }
    } else {
      title = r.winner ? `${names[r.winner].name} Won!` : 'Draw';
      if (!r.winner) cls = 'draw';
    }
    el.modalTitle.textContent = title;
    el.modalSub.textContent = reasonText(r.reason);
    el.modalHeader.className = 'modal-header ' + cls;
    el.modalScore.textContent = r.winner === 'w' ? '1 - 0' : r.winner === 'b' ? '0 - 1' : '½ - ½';
    el.modalNameW.textContent = names.w.name;
    el.modalNameB.textContent = names.b.name;
    el.modalAvW.className = 'avatar ' + names.w.cls; el.modalAvW.textContent = names.w.initial;
    el.modalAvB.className = 'avatar ' + names.b.cls; el.modalAvB.textContent = names.b.initial;
    el.modal.classList.remove('hidden');
  }
  function hideModal() { el.modal.classList.add('hidden'); }

  function resignOrAbort() {
    if (state.status !== 'playing') return;
    if (state.game.history.length < 2) { endGame(null, 'aborted'); toast('Game aborted'); return; }
    const loser = state.mode === 'friend' ? state.game.turn : state.playerColor;
    endGame(opp(loser), 'resignation');
  }

  function offerDraw() {
    if (state.status !== 'playing') return;
    if (state.mode === 'friend') { endGame(null, 'agreement'); return; }
    const bal = state.game.materialBalance();
    const botAhead = state.playerColor === 'w' ? bal < 0 : bal > 0;
    if (!botAhead) endGame(null, 'agreement');
    else toast((state.mode === 'engine' ? 'Stockfish' : 'Computer') + ' declined the draw offer');
  }

  async function takeback() {
    if (state.status !== 'playing' || !state.game.history.length || state.thinking) return;
    const g = state.game;
    clearTimeout(state.botTimer);
    state.gameId++;
    let n = 1;
    if (state.mode !== 'friend' && g.turn === state.playerColor && g.history.length >= 2) n = 2;
    if (state.mode === 'engine') {
      try {
        const res = await Backend.undo();
        if (typeof res.undone === 'number') n = res.undone;
      } catch (e) {
        toast('Takeback failed: ' + e.message, 4000);
        return;
      }
    }
    for (let i = 0; i < n && g.history.length; i++) { g.undo(); state.snapshots.pop(); }
    state.selected = null; state.legal = []; state.viewPly = null; state.pendingPromo = null;
    el.promo.classList.add('hidden');
    if (state.tc.base && g.history.length) { state.running = g.turn; state.lastTick = performance.now(); }
    else state.running = null;
    renderAll();
    if (state.mode === 'computer' && g.turn !== state.playerColor) scheduleBot();
  }

  function setViewPly(n) {
    const plies = state.game.history.length;
    n = Math.max(0, Math.min(plies, n));
    state.viewPly = n >= plies ? null : n;
    state.selected = null; state.legal = [];
    renderBoard();
    renderMoves();
    renderControls();
  }
  function goLive() { if (state.viewPly !== null) setViewPly(state.game.history.length); }

  function flip() {
    state.orientation = opp(state.orientation);
    renderFrameLabels();
    renderBoard();
    renderPlayers();
  }

  // ----------------------------------------------------------------- clock

  function tick() {
    if (state.status !== 'playing' || !state.running || !state.tc.base) return;
    const now = performance.now();
    state.clocks[state.running] -= now - state.lastTick;
    state.lastTick = now;
    if (state.clocks[state.running] <= 0) {
      state.clocks[state.running] = 0;
      const loser = state.running;
      const winner = opp(loser);
      renderClocks();
      if (state.game.hasMatingMaterial(winner)) endGame(winner, 'timeout');
      else endGame(null, 'timeout vs insufficient material');
      return;
    }
    renderClocks();
  }
  setInterval(tick, 100);

  // ----------------------------------------------------------------- promotion

  function showPromotion(from, to, cands, opts) {
    state.pendingPromo = { from, to, cands, opts };
    const [x, y] = toXY(to);
    const color = colorOf(state.game.board[from]);
    el.promo.className = 'promo' + (y === 7 ? ' bottom' : '');
    el.promo.style.left = `${x * 12.5}%`;
    el.promo.innerHTML = ['q', 'n', 'r', 'b'].map((t) =>
      `<button type="button" data-promo="${t}" style="background-image:url('${pieceAsset(color + t.toUpperCase())}')" title="${t}"></button>`).join('') +
      '<button type="button" class="x" data-promo="cancel">×</button>';
    renderBoard();
  }

  el.promo.addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b || !state.pendingPromo) return;
    const pp = state.pendingPromo;
    if (b.dataset.promo === 'cancel') {
      state.pendingPromo = null;
      el.promo.classList.add('hidden');
      deselect();
      renderBoard();
      return;
    }
    const m = pp.cands.find((c) => c.promo === b.dataset.promo);
    commitMove(m, pp.opts);
  });

  // ----------------------------------------------------------------- pointer interaction

  function squareFromEvent(e) {
    const rect = el.board.getBoundingClientRect();
    const x = Math.floor(((e.clientX - rect.left) / rect.width) * 8);
    const y = Math.floor(((e.clientY - rect.top) / rect.height) * 8);
    if (x < 0 || x > 7 || y < 0 || y > 7) return -1;
    return fromXY(x, y);
  }

  el.board.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    if (state.pendingPromo) {
      if (!e.target.closest('.promo')) {
        state.pendingPromo = null; el.promo.classList.add('hidden'); deselect(); renderBoard();
      }
      return;
    }
    if (state.viewPly !== null) goLive();
    if (!canInteract()) return;
    const sqi = squareFromEvent(e);
    if (sqi < 0) return;
    const p = state.game.board[sqi];

    if (state.selected !== null && state.legal.some((m) => m.to === sqi)) {
      tryMove(state.selected, sqi, { animate: true, source: 'human' });
      return;
    }
    if (p && colorOf(p) === state.game.turn) {
      const wasSelected = state.selected === sqi;
      select(sqi);
      startDrag(e, sqi, wasSelected);
    } else {
      if (state.selected !== null && p) Sound.play('illegal');
      deselect();
    }
  });

  function startDrag(e, sqi, wasSelected) {
    const piece = el.pieces.querySelector(`[data-sq="${sqi}"]`);
    if (!piece) return;
    try { el.board.setPointerCapture(e.pointerId); } catch (err) { /* ignore */ }
    state.drag = { sqi, piece, startX: e.clientX, startY: e.clientY, moved: false, wasSelected };
    piece.classList.add('dragging');
    el.board.classList.add('dragging');
    positionDrag(e);
  }

  function positionDrag(e) {
    const rect = el.board.getBoundingClientRect();
    const size = rect.width / 8;
    const x = e.clientX - rect.left - size / 2;
    const y = e.clientY - rect.top - size / 2;
    state.drag.piece.style.transform = `translate(${x}px, ${y}px)`;
  }

  el.board.addEventListener('pointermove', (e) => {
    const d = state.drag;
    if (!d) return;
    if (!d.moved && Math.hypot(e.clientX - d.startX, e.clientY - d.startY) > 4) d.moved = true;
    positionDrag(e);
    const over = squareFromEvent(e);
    if (over !== state.hoverSq) {
      const prev = el.squares.querySelector('.sq.hover');
      if (prev) prev.classList.remove('hover');
      state.hoverSq = over;
      if (over >= 0 && state.legal.some((m) => m.to === over)) {
        const c = el.squares.querySelector(`.sq[data-sq="${over}"]`);
        if (c) c.classList.add('hover');
      }
    }
  });

  function endDrag(e, cancelled) {
    const d = state.drag;
    if (!d) return;
    state.drag = null;
    state.hoverSq = null;
    d.piece.classList.remove('dragging');
    el.board.classList.remove('dragging');
    try { el.board.releasePointerCapture(e.pointerId); } catch (err) { /* ignore */ }
    const target = cancelled ? -1 : squareFromEvent(e);
    if (d.moved && target >= 0 && target !== d.sqi && state.legal.some((m) => m.to === target)) {
      tryMove(d.sqi, target, { animate: false, source: 'human' });
    } else if (!d.moved && d.wasSelected) {
      deselect();
    } else {
      renderBoard(); // snap back
    }
  }
  el.board.addEventListener('pointerup', (e) => endDrag(e, false));
  el.board.addEventListener('pointercancel', (e) => endDrag(e, true));

  // ----------------------------------------------------------------- UI wiring

  el.tcButton.addEventListener('click', () => {
    const open = el.tcMenu.classList.toggle('hidden');
    el.tcButton.setAttribute('aria-expanded', String(!open));
  });
  el.tcMenu.addEventListener('click', (e) => {
    const b = e.target.closest('.tc-opt');
    if (!b) return;
    state.tc = TIME_CONTROLS.find((t) => t.id === b.dataset.tc);
    save();
    renderNewGamePanel();
    el.tcMenu.classList.add('hidden');
    el.tcButton.setAttribute('aria-expanded', 'false');
  });
  el.levelPick.addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    state.level = b.dataset.level;
    save();
    renderNewGamePanel();
  });
  el.colorPick.addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    state.colorPref = b.dataset.color;
    save();
    renderNewGamePanel();
  });
  const startComputerGame = () => startGame(Backend.available ? 'engine' : 'computer');
  el.playBtn.addEventListener('click', startComputerGame);
  el.playBot.addEventListener('click', startComputerGame);
  el.playFriend.addEventListener('click', () => startGame('friend'));

  el.btnResign.addEventListener('click', () => {
    if (state.status !== 'playing') return;
    if (state.game.history.length < 2) { resignOrAbort(); return; }
    el.confirmText.textContent = 'Are you sure you want to resign?';
    el.confirm.classList.remove('hidden');
    el.confirm.dataset.action = 'resign';
  });
  el.btnDraw.addEventListener('click', () => {
    if (state.status !== 'playing') return;
    if (state.mode === 'friend') {
      el.confirmText.textContent = 'Agree to a draw?';
      el.confirm.classList.remove('hidden');
      el.confirm.dataset.action = 'draw';
    } else offerDraw();
  });
  el.confirmYes.addEventListener('click', () => {
    el.confirm.classList.add('hidden');
    if (el.confirm.dataset.action === 'resign') resignOrAbort();
    else offerDraw();
  });
  el.confirmNo.addEventListener('click', () => el.confirm.classList.add('hidden'));
  el.btnTakeback.addEventListener('click', takeback);
  el.btnFlip.addEventListener('click', flip);
  el.navFirst.addEventListener('click', () => setViewPly(0));
  el.navPrev.addEventListener('click', () => setViewPly((state.viewPly === null ? state.game.history.length : state.viewPly) - 1));
  el.navNext.addEventListener('click', () => setViewPly((state.viewPly === null ? state.game.history.length : state.viewPly) + 1));
  el.navLast.addEventListener('click', () => setViewPly(state.game.history.length));
  el.moves.addEventListener('click', (e) => {
    const mv = e.target.closest('.mv');
    if (mv) setViewPly(parseInt(mv.dataset.ply, 10));
  });

  function rematch() {
    const nextColor = state.mode === 'computer' ? opp(state.playerColor) : 'w';
    startGame(state.mode, nextColor);
  }
  function newGame() {
    clearTimeout(state.botTimer);
    state.gameId++;
    state.status = 'idle';
    state.running = null;
    state.thinking = false;
    hideModal();
    el.panelGame.classList.add('hidden');
    el.panelNew.classList.remove('hidden');
    renderNewGamePanel();
    renderControls();
    renderPlayers();
  }
  el.btnRematch.addEventListener('click', rematch);
  el.modalRematch.addEventListener('click', rematch);
  el.btnNewGame.addEventListener('click', newGame);
  el.modalNew.addEventListener('click', newGame);
  el.modalClose.addEventListener('click', hideModal);
  el.modal.addEventListener('click', (e) => { if (e.target === el.modal) hideModal(); });

  function copy(text, label) {
    const done = () => toast(`${label} copied`);
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, () => toast('Copy failed'));
    else { window.prompt(label, text); }
  }
  el.btnPgn.addEventListener('click', () => {
    const names = playerNames();
    const r = state.result;
    copy(state.game.pgn({
      Event: 'DUEN chess', Site: location.host || 'local', Date: new Date().toISOString().slice(0, 10).replace(/-/g, '.'),
      White: names.w.name, Black: names.b.name,
      Result: r ? (r.winner === 'w' ? '1-0' : r.winner === 'b' ? '0-1' : '1/2-1/2') : '*',
      TimeControl: state.tc.base ? `${state.tc.base}+${state.tc.inc}` : '-',
    }), 'PGN');
  });
  el.btnFen.addEventListener('click', () => copy(state.game.fen(), 'FEN'));

  el.soundToggle.addEventListener('click', () => {
    state.sound = !state.sound;
    save();
    el.soundToggle.classList.toggle('off', !state.sound);
    if (state.sound) Sound.play('move');
  });

  document.addEventListener('keydown', (e) => {
    if (e.target.matches('input, textarea, select')) return;
    const plies = state.game.history.length;
    const cur = state.viewPly === null ? plies : state.viewPly;
    switch (e.key) {
      case 'ArrowLeft': setViewPly(cur - 1); e.preventDefault(); break;
      case 'ArrowRight': setViewPly(cur + 1); e.preventDefault(); break;
      case 'ArrowUp': case 'Home': setViewPly(0); e.preventDefault(); break;
      case 'ArrowDown': case 'End': setViewPly(plies); e.preventDefault(); break;
      case 'f': case 'F': flip(); break;
      case 'Escape': hideModal(); el.confirm.classList.add('hidden'); if (state.pendingPromo) { state.pendingPromo = null; el.promo.classList.add('hidden'); deselect(); renderBoard(); } break;
    }
  });

  document.addEventListener('click', (e) => {
    if (!e.target.closest('.tc-picker')) {
      el.tcMenu.classList.add('hidden');
      el.tcButton.setAttribute('aria-expanded', 'false');
    }
  });

  // ----------------------------------------------------------------- boot

  restore();
  applyPieceStyle();
  buildSquares();
  pushSnapshot(null);
  renderNewGamePanel();
  renderAll();
  Backend.probe().then(() => {
    if (state.status !== 'idle') return;
    if (Backend.available) state.mode = 'engine';
    renderNewGamePanel();
    renderAll();
  });
})();
