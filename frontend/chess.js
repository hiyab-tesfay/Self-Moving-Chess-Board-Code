/*
 * chess.js — a self-contained chess rules engine (no dependencies).
 *
 * Board: 64-element array, index = rank * 8 + file, a1 = 0, h8 = 63.
 * Pieces: single FEN characters ('P','N','B','R','Q','K' white; lowercase black).
 * Moves:  { from, to, flags, promo } where promo is 'q' | 'r' | 'b' | 'n' | null.
 *
 * Works in the browser (global `Chess`) and in Node (module.exports).
 */
(function (root) {
  'use strict';

  const FILES = 'abcdefgh';
  const START_FEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1';

  // Move flags (bit field)
  const F_NORMAL = 1;
  const F_CAPTURE = 2;
  const F_DOUBLE = 4;     // pawn double push
  const F_EP = 8;         // en passant capture
  const F_CASTLE_K = 16;  // king-side castle
  const F_CASTLE_Q = 32;  // queen-side castle
  const F_PROMO = 64;     // promotion

  const KNIGHT_D = [[1, 2], [2, 1], [2, -1], [1, -2], [-1, -2], [-2, -1], [-2, 1], [-1, 2]];
  const KING_D = [[1, 0], [1, 1], [0, 1], [-1, 1], [-1, 0], [-1, -1], [0, -1], [1, -1]];
  const ROOK_D = [[1, 0], [-1, 0], [0, 1], [0, -1]];
  const BISHOP_D = [[1, 1], [1, -1], [-1, 1], [-1, -1]];

  const sq = (f, r) => r * 8 + f;
  const fileOf = (i) => i & 7;
  const rankOf = (i) => i >> 3;
  const sqName = (i) => FILES[i & 7] + ((i >> 3) + 1);
  const parseSq = (s) => sq(FILES.indexOf(s[0]), parseInt(s[1], 10) - 1);
  const colorOf = (p) => (p < 'a' ? 'w' : 'b');
  const typeOf = (p) => p.toLowerCase();
  const opp = (c) => (c === 'w' ? 'b' : 'w');
  const makePiece = (type, color) => (color === 'w' ? type.toUpperCase() : type);
  const onBoard = (f, r) => f >= 0 && f < 8 && r >= 0 && r < 8;

  class Chess {
    constructor(fen) {
      this.load(fen || START_FEN);
    }

    // ---------------------------------------------------------------- setup

    load(fen) {
      const parts = fen.trim().split(/\s+/);
      this.board = new Array(64).fill(null);
      this.kings = { w: -1, b: -1 };
      const rows = parts[0].split('/');
      if (rows.length !== 8) throw new Error('Invalid FEN: ' + fen);
      for (let r = 0; r < 8; r++) {
        const rank = 7 - r;
        let file = 0;
        for (const ch of rows[r]) {
          if (ch >= '1' && ch <= '8') {
            file += parseInt(ch, 10);
          } else {
            const i = sq(file, rank);
            this.board[i] = ch;
            if (ch === 'K') this.kings.w = i;
            if (ch === 'k') this.kings.b = i;
            file++;
          }
        }
      }
      this.turn = parts[1] === 'b' ? 'b' : 'w';
      const c = parts[2] || '-';
      this.castling = {
        wK: c.includes('K'), wQ: c.includes('Q'),
        bK: c.includes('k'), bQ: c.includes('q'),
      };
      this.ep = parts[3] && parts[3] !== '-' ? parseSq(parts[3]) : -1;
      this.halfmove = parseInt(parts[4] || '0', 10);
      this.fullmove = parseInt(parts[5] || '1', 10);
      this.startFen = this.fen();
      this.history = [];
      this.positions = Object.create(null);
      this.positions[this.positionKey()] = 1;
      return this;
    }

    reset() { return this.load(START_FEN); }

    fen() {
      let out = '';
      for (let rank = 7; rank >= 0; rank--) {
        let empty = 0;
        for (let file = 0; file < 8; file++) {
          const p = this.board[sq(file, rank)];
          if (!p) { empty++; continue; }
          if (empty) { out += empty; empty = 0; }
          out += p;
        }
        if (empty) out += empty;
        if (rank) out += '/';
      }
      const c = (this.castling.wK ? 'K' : '') + (this.castling.wQ ? 'Q' : '') +
                (this.castling.bK ? 'k' : '') + (this.castling.bQ ? 'q' : '');
      return `${out} ${this.turn} ${c || '-'} ${this.ep >= 0 ? sqName(this.ep) : '-'} ${this.halfmove} ${this.fullmove}`;
    }

    /** Key identifying the position for repetition purposes. */
    positionKey() {
      let s = '';
      for (let i = 0; i < 64; i++) s += this.board[i] || '.';
      s += this.turn;
      s += (this.castling.wK ? 'K' : '') + (this.castling.wQ ? 'Q' : '') +
           (this.castling.bK ? 'k' : '') + (this.castling.bQ ? 'q' : '');
      s += this.ep >= 0 ? sqName(this.ep) : '-';
      return s;
    }

    get(square) {
      const i = typeof square === 'string' ? parseSq(square) : square;
      const p = this.board[i];
      return p ? { type: typeOf(p), color: colorOf(p) } : null;
    }

    // ------------------------------------------------------------- attacks

    /** Is `target` attacked by any piece of color `by`? */
    isAttacked(target, by) {
      const b = this.board;
      const tf = fileOf(target), tr = rankOf(target);

      // Pawns
      const pr = by === 'w' ? tr - 1 : tr + 1;
      if (pr >= 0 && pr < 8) {
        const pawn = by === 'w' ? 'P' : 'p';
        if (tf > 0 && b[sq(tf - 1, pr)] === pawn) return true;
        if (tf < 7 && b[sq(tf + 1, pr)] === pawn) return true;
      }
      // Knights
      const knight = by === 'w' ? 'N' : 'n';
      for (let k = 0; k < 8; k++) {
        const f = tf + KNIGHT_D[k][0], r = tr + KNIGHT_D[k][1];
        if (onBoard(f, r) && b[sq(f, r)] === knight) return true;
      }
      // King
      const king = by === 'w' ? 'K' : 'k';
      for (let k = 0; k < 8; k++) {
        const f = tf + KING_D[k][0], r = tr + KING_D[k][1];
        if (onBoard(f, r) && b[sq(f, r)] === king) return true;
      }
      // Sliders
      const rook = by === 'w' ? 'R' : 'r';
      const bishop = by === 'w' ? 'B' : 'b';
      const queen = by === 'w' ? 'Q' : 'q';
      for (let k = 0; k < 4; k++) {
        let f = tf + ROOK_D[k][0], r = tr + ROOK_D[k][1];
        while (onBoard(f, r)) {
          const p = b[sq(f, r)];
          if (p) { if (p === rook || p === queen) return true; break; }
          f += ROOK_D[k][0]; r += ROOK_D[k][1];
        }
      }
      for (let k = 0; k < 4; k++) {
        let f = tf + BISHOP_D[k][0], r = tr + BISHOP_D[k][1];
        while (onBoard(f, r)) {
          const p = b[sq(f, r)];
          if (p) { if (p === bishop || p === queen) return true; break; }
          f += BISHOP_D[k][0]; r += BISHOP_D[k][1];
        }
      }
      return false;
    }

    inCheck() {
      return this.isAttacked(this.kings[this.turn], opp(this.turn));
    }

    // ------------------------------------------------------ move generation

    /** Pseudo-legal moves for `color` (default: side to move). */
    pseudoMoves(color) {
      color = color || this.turn;
      const b = this.board;
      const moves = [];
      const add = (from, to, flags, promo) => moves.push({ from, to, flags, promo: promo || null });

      for (let from = 0; from < 64; from++) {
        const p = b[from];
        if (!p || colorOf(p) !== color) continue;
        const t = typeOf(p), f = fileOf(from), r = rankOf(from);

        if (t === 'p') {
          const dir = color === 'w' ? 1 : -1;
          const startRank = color === 'w' ? 1 : 6;
          const promoRank = color === 'w' ? 7 : 0;
          const r1 = r + dir;
          if (r1 < 0 || r1 > 7) continue;
          const to = sq(f, r1);
          if (!b[to]) {
            if (r1 === promoRank) {
              for (const pr of 'qrbn') add(from, to, F_PROMO, pr);
            } else {
              add(from, to, F_NORMAL);
              if (r === startRank && !b[sq(f, r + 2 * dir)]) add(from, sq(f, r + 2 * dir), F_DOUBLE);
            }
          }
          for (let df = -1; df <= 1; df += 2) {
            const cf = f + df;
            if (cf < 0 || cf > 7) continue;
            const cto = sq(cf, r1);
            const cp = b[cto];
            if (cp && colorOf(cp) !== color) {
              if (r1 === promoRank) {
                for (const pr of 'qrbn') add(from, cto, F_CAPTURE | F_PROMO, pr);
              } else {
                add(from, cto, F_CAPTURE);
              }
            } else if (!cp && cto === this.ep) {
              add(from, cto, F_CAPTURE | F_EP);
            }
          }
        } else if (t === 'n' || t === 'k') {
          const D = t === 'n' ? KNIGHT_D : KING_D;
          for (let k = 0; k < 8; k++) {
            const nf = f + D[k][0], nr = r + D[k][1];
            if (!onBoard(nf, nr)) continue;
            const to = sq(nf, nr), q = b[to];
            if (!q) add(from, to, F_NORMAL);
            else if (colorOf(q) !== color) add(from, to, F_CAPTURE);
          }
          if (t === 'k') {
            const home = color === 'w' ? 4 : 60;
            if (from === home) {
              const enemy = opp(color);
              const rookPiece = makePiece('r', color);
              if (this.castling[color + 'K'] &&
                  !b[home + 1] && !b[home + 2] && b[home + 3] === rookPiece &&
                  !this.isAttacked(home, enemy) && !this.isAttacked(home + 1, enemy) && !this.isAttacked(home + 2, enemy)) {
                add(from, home + 2, F_CASTLE_K);
              }
              if (this.castling[color + 'Q'] &&
                  !b[home - 1] && !b[home - 2] && !b[home - 3] && b[home - 4] === rookPiece &&
                  !this.isAttacked(home, enemy) && !this.isAttacked(home - 1, enemy) && !this.isAttacked(home - 2, enemy)) {
                add(from, home - 2, F_CASTLE_Q);
              }
            }
          }
        } else {
          const D = t === 'r' ? ROOK_D : t === 'b' ? BISHOP_D : KING_D; // queen = all 8 king directions
          for (let k = 0; k < D.length; k++) {
            let nf = f + D[k][0], nr = r + D[k][1];
            while (onBoard(nf, nr)) {
              const to = sq(nf, nr), q = b[to];
              if (!q) add(from, to, F_NORMAL);
              else { if (colorOf(q) !== color) add(from, to, F_CAPTURE); break; }
              nf += D[k][0]; nr += D[k][1];
            }
          }
        }
      }
      return moves;
    }

    /** Fully legal moves for the side to move. */
    legalMoves() {
      const color = this.turn, enemy = opp(color);
      const out = [];
      const pseudo = this.pseudoMoves(color);
      for (let i = 0; i < pseudo.length; i++) {
        const m = pseudo[i];
        const u = this.make(m);
        if (!this.isAttacked(this.kings[color], enemy)) out.push(m);
        this.unmake(u);
      }
      return out;
    }

    /** Legal moves originating from one square. */
    movesFrom(square) {
      const i = typeof square === 'string' ? parseSq(square) : square;
      return this.legalMoves().filter((m) => m.from === i);
    }

    // ------------------------------------------------------ make / unmake

    /** Apply a (pseudo-)legal move. Returns an undo record. Does not touch history. */
    make(m) {
      const b = this.board;
      const piece = b[m.from];
      const color = colorOf(piece);
      const type = typeOf(piece);
      const u = {
        m,
        captured: null,
        castling: this.castling,
        ep: this.ep,
        halfmove: this.halfmove,
        kings: this.kings,
      };
      this.castling = { wK: this.castling.wK, wQ: this.castling.wQ, bK: this.castling.bK, bQ: this.castling.bQ };
      this.kings = { w: this.kings.w, b: this.kings.b };

      let captured = b[m.to];
      if (m.flags & F_EP) {
        const cs = m.to + (color === 'w' ? -8 : 8);
        captured = b[cs];
        b[cs] = null;
      }
      u.captured = captured;

      b[m.to] = m.promo ? makePiece(m.promo, color) : piece;
      b[m.from] = null;

      if (m.flags & F_CASTLE_K) { b[m.to - 1] = b[m.to + 1]; b[m.to + 1] = null; }
      else if (m.flags & F_CASTLE_Q) { b[m.to + 1] = b[m.to - 2]; b[m.to - 2] = null; }

      if (type === 'k') {
        this.kings[color] = m.to;
        this.castling[color + 'K'] = false;
        this.castling[color + 'Q'] = false;
      } else if (type === 'r') {
        if (m.from === (color === 'w' ? 7 : 63)) this.castling[color + 'K'] = false;
        if (m.from === (color === 'w' ? 0 : 56)) this.castling[color + 'Q'] = false;
      }
      if (captured && typeOf(captured) === 'r') {
        const ec = opp(color);
        if (m.to === (ec === 'w' ? 7 : 63)) this.castling[ec + 'K'] = false;
        if (m.to === (ec === 'w' ? 0 : 56)) this.castling[ec + 'Q'] = false;
      }

      this.ep = -1;
      if (m.flags & F_DOUBLE) {
        // Only record an en-passant square when an enemy pawn could actually use it.
        const epawn = makePiece('p', opp(color));
        const f = fileOf(m.to);
        if ((f > 0 && b[m.to - 1] === epawn) || (f < 7 && b[m.to + 1] === epawn)) {
          this.ep = (m.from + m.to) >> 1;
        }
      }

      this.halfmove = (type === 'p' || captured) ? 0 : this.halfmove + 1;
      if (color === 'b') this.fullmove++;
      this.turn = opp(color);
      return u;
    }

    unmake(u) {
      const m = u.m;
      const b = this.board;
      this.turn = opp(this.turn);
      const color = this.turn;
      const moved = b[m.to];
      b[m.from] = m.promo ? makePiece('p', color) : moved;
      b[m.to] = null;
      if (m.flags & F_EP) b[m.to + (color === 'w' ? -8 : 8)] = u.captured;
      else if (u.captured) b[m.to] = u.captured;
      if (m.flags & F_CASTLE_K) { b[m.to + 1] = b[m.to - 1]; b[m.to - 1] = null; }
      else if (m.flags & F_CASTLE_Q) { b[m.to - 2] = b[m.to + 1]; b[m.to + 1] = null; }
      this.castling = u.castling;
      this.ep = u.ep;
      this.halfmove = u.halfmove;
      this.kings = u.kings;
      if (color === 'b') this.fullmove--;
    }

    // ------------------------------------------------------------- game API

    /**
     * Play a move and record it in history.
     * Accepts a move object ({from, to, promo}) with numeric or algebraic squares,
     * a UCI string like "e2e4" / "e7e8q", or a SAN string like "Nf3".
     * Returns the history record, or null if the move is illegal.
     */
    move(input) {
      const legal = this.legalMoves();
      let m = null;
      if (typeof input === 'string') {
        const s = input.trim();
        if (/^[a-h][1-8][a-h][1-8][qrbn]?$/.test(s)) {
          const from = parseSq(s.slice(0, 2)), to = parseSq(s.slice(2, 4)), promo = s[4] || null;
          m = legal.find((x) => x.from === from && x.to === to && (x.promo || null) === promo) || null;
        } else {
          const clean = s.replace(/[+#?!]+$/, '');
          m = legal.find((x) => this.san(x).replace(/[+#]+$/, '') === clean) || null;
        }
      } else if (input) {
        const from = typeof input.from === 'string' ? parseSq(input.from) : input.from;
        const to = typeof input.to === 'string' ? parseSq(input.to) : input.to;
        const promo = input.promo ? input.promo.toLowerCase() : null;
        m = legal.find((x) => x.from === from && x.to === to && (x.promo || null) === promo) || null;
      }
      if (!m) return null;

      const san = this.san(m);
      const piece = this.board[m.from];
      const u = this.make(m);
      const key = this.positionKey();
      this.positions[key] = (this.positions[key] || 0) + 1;
      const rec = {
        move: m,
        san,
        uci: sqName(m.from) + sqName(m.to) + (m.promo || ''),
        piece,
        color: colorOf(piece),
        captured: u.captured,
        undo: u,
        key,
        fen: this.fen(),
        check: this.inCheck(),
      };
      this.history.push(rec);
      return rec;
    }

    /** Take back the last move. Returns the record, or null. */
    undo() {
      const rec = this.history.pop();
      if (!rec) return null;
      this.positions[rec.key]--;
      this.unmake(rec.undo);
      return rec;
    }

    /** Standard algebraic notation for a legal move in the current position. */
    san(m) {
      const piece = this.board[m.from];
      const type = typeOf(piece);
      let s;
      if (m.flags & F_CASTLE_K) s = 'O-O';
      else if (m.flags & F_CASTLE_Q) s = 'O-O-O';
      else {
        const capture = !!(m.flags & F_CAPTURE);
        if (type === 'p') {
          s = (capture ? FILES[fileOf(m.from)] + 'x' : '') + sqName(m.to);
          if (m.promo) s += '=' + m.promo.toUpperCase();
        } else {
          s = type.toUpperCase();
          const others = this.legalMoves().filter((x) =>
            x.to === m.to && x.from !== m.from && typeOf(this.board[x.from]) === type);
          if (others.length) {
            const sameFile = others.some((x) => fileOf(x.from) === fileOf(m.from));
            const sameRank = others.some((x) => rankOf(x.from) === rankOf(m.from));
            if (!sameFile) s += FILES[fileOf(m.from)];
            else if (!sameRank) s += rankOf(m.from) + 1;
            else s += sqName(m.from);
          }
          if (capture) s += 'x';
          s += sqName(m.to);
        }
      }
      const u = this.make(m);
      if (this.inCheck()) s += this.legalMoves().length === 0 ? '#' : '+';
      this.unmake(u);
      return s;
    }

    isCheckmate() { return this.inCheck() && this.legalMoves().length === 0; }
    isStalemate() { return !this.inCheck() && this.legalMoves().length === 0; }
    isThreefoldRepetition() { return (this.positions[this.positionKey()] || 0) >= 3; }
    isFiftyMoveRule() { return this.halfmove >= 100; }

    /** Neither side can possibly checkmate. */
    isInsufficientMaterial() {
      const pieces = [];
      for (let i = 0; i < 64; i++) {
        const p = this.board[i];
        if (p && typeOf(p) !== 'k') pieces.push({ t: typeOf(p), i });
      }
      if (pieces.length === 0) return true;
      if (pieces.length === 1) return pieces[0].t === 'b' || pieces[0].t === 'n';
      if (pieces.every((x) => x.t === 'b')) {
        const shade = (fileOf(pieces[0].i) + rankOf(pieces[0].i)) & 1;
        return pieces.every((x) => ((fileOf(x.i) + rankOf(x.i)) & 1) === shade);
      }
      return false;
    }

    /** Can `color` still deliver mate (used for flag falls)? Lone K, K+B or K+N cannot. */
    hasMatingMaterial(color) {
      let minors = 0;
      for (let i = 0; i < 64; i++) {
        const p = this.board[i];
        if (!p || colorOf(p) !== color) continue;
        const t = typeOf(p);
        if (t === 'p' || t === 'r' || t === 'q') return true;
        if (t === 'b' || t === 'n') minors++;
      }
      return minors >= 2;
    }

    isDraw() {
      return this.isStalemate() || this.isThreefoldRepetition() || this.isFiftyMoveRule() || this.isInsufficientMaterial();
    }

    /**
     * Terminal state of the game, or null if play continues.
     * { result: '1-0' | '0-1' | '1/2-1/2', winner: 'w' | 'b' | null, reason }
     */
    gameOver() {
      const moves = this.legalMoves();
      if (moves.length === 0) {
        if (this.inCheck()) {
          const winner = opp(this.turn);
          return { result: winner === 'w' ? '1-0' : '0-1', winner, reason: 'checkmate' };
        }
        return { result: '1/2-1/2', winner: null, reason: 'stalemate' };
      }
      if (this.isInsufficientMaterial()) return { result: '1/2-1/2', winner: null, reason: 'insufficient material' };
      if (this.isThreefoldRepetition()) return { result: '1/2-1/2', winner: null, reason: 'threefold repetition' };
      if (this.isFiftyMoveRule()) return { result: '1/2-1/2', winner: null, reason: '50-move rule' };
      return null;
    }

    /** PGN export. */
    pgn(headers) {
      headers = headers || {};
      let out = '';
      for (const k of Object.keys(headers)) out += `[${k} "${headers[k]}"]\n`;
      if (out) out += '\n';
      const toks = [];
      this.history.forEach((h, i) => {
        if (i % 2 === 0) toks.push(`${i / 2 + 1}.`);
        toks.push(h.san);
      });
      if (headers.Result) toks.push(headers.Result);
      // wrap at ~80 columns
      const lines = [];
      let line = '';
      for (const t of toks) {
        if (line.length + t.length + 1 > 80) { lines.push(line); line = t; }
        else line = line ? line + ' ' + t : t;
      }
      if (line) lines.push(line);
      return out + lines.join('\n');
    }

    /** Material balance in pawns (positive = white ahead). */
    materialBalance() {
      const v = { p: 1, n: 3, b: 3, r: 5, q: 9, k: 0 };
      let s = 0;
      for (let i = 0; i < 64; i++) {
        const p = this.board[i];
        if (p) s += (colorOf(p) === 'w' ? 1 : -1) * v[typeOf(p)];
      }
      return s;
    }

    /** Pieces each side has captured, derived from what is missing from the board. */
    capturedPieces() {
      const start = { p: 8, n: 2, b: 2, r: 2, q: 1 };
      const on = { w: { p: 0, n: 0, b: 0, r: 0, q: 0 }, b: { p: 0, n: 0, b: 0, r: 0, q: 0 } };
      for (let i = 0; i < 64; i++) {
        const p = this.board[i];
        if (p && typeOf(p) !== 'k') on[colorOf(p)][typeOf(p)]++;
      }
      const res = { w: [], b: [] };
      for (const t of ['p', 'n', 'b', 'r', 'q']) {
        for (let k = 0; k < Math.max(0, start[t] - on.b[t]); k++) res.w.push(t); // white captured black pieces
        for (let k = 0; k < Math.max(0, start[t] - on.w[t]); k++) res.b.push(t);
      }
      return res;
    }

    /** Perft: number of leaf nodes at `depth` (for testing move generation). */
    perft(depth) {
      if (depth === 0) return 1;
      const moves = this.legalMoves();
      if (depth === 1) return moves.length;
      let n = 0;
      for (const m of moves) {
        const u = this.make(m);
        n += this.perft(depth - 1);
        this.unmake(u);
      }
      return n;
    }

    ascii() {
      let s = '';
      for (let r = 7; r >= 0; r--) {
        s += (r + 1) + ' ';
        for (let f = 0; f < 8; f++) s += (this.board[sq(f, r)] || '.') + ' ';
        s += '\n';
      }
      return s + '  a b c d e f g h';
    }
  }

  Chess.FLAGS = { NORMAL: F_NORMAL, CAPTURE: F_CAPTURE, DOUBLE: F_DOUBLE, EP: F_EP, CASTLE_K: F_CASTLE_K, CASTLE_Q: F_CASTLE_Q, PROMO: F_PROMO };
  Chess.START_FEN = START_FEN;
  Chess.sqName = sqName;
  Chess.parseSq = parseSq;
  Chess.fileOf = fileOf;
  Chess.rankOf = rankOf;
  Chess.colorOf = colorOf;
  Chess.typeOf = typeOf;
  Chess.opp = opp;

  if (typeof module !== 'undefined' && module.exports) module.exports = Chess;
  else root.Chess = Chess;
})(typeof self !== 'undefined' ? self : this);
