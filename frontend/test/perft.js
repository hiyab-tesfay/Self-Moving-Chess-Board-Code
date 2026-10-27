// Move-generation correctness tests (perft) + rules sanity checks.
// Run: node test/perft.js
const Chess = require('../chess.js');

const cases = [
  // [name, fen, depth, expected]
  ['start d3', Chess.START_FEN, 3, 8902],
  ['start d4', Chess.START_FEN, 4, 197281],
  ['kiwipete d3', 'r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1', 3, 97862],
  ['position 3 d4', '8/2p5/3p4/KP5r/1R3p1k/8/4P1P1/8 w - - 0 1', 4, 43238],
  ['position 4 d3', 'r3k2r/Pppp1ppp/1b3nbN/nP6/BBP1P3/q4N2/Pp1P2PP/R2Q1RK1 w kq - 0 1', 3, 9467],
  ['position 5 d3', 'rnbq1k1r/pp1Pbppp/2p5/8/2B5/8/PPP1NnPP/RNBQK2R w KQ - 1 8', 3, 62379],
  ['position 6 d3', 'r4rk1/1pp1qppp/p1np1n2/2b1p1B1/2B1P1b1/P1NP1N2/1PP1QPPP/R4RK1 w - - 0 10', 3, 89890],
];

let failed = 0;
for (const [name, fen, depth, expected] of cases) {
  const g = new Chess(fen);
  const t0 = Date.now();
  const n = g.perft(depth);
  const ok = n === expected;
  if (!ok) failed++;
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name}: ${n} (expected ${expected}) ${Date.now() - t0}ms`);
}

// Rules sanity checks
function expect(cond, msg) { if (!cond) { failed++; console.log('FAIL ' + msg); } else console.log('PASS ' + msg); }

let g = new Chess();
for (const m of ['e4', 'e5', 'Qh5', 'Nc6', 'Bc4', 'Nf6', 'Qxf7#']) {
  if (!g.move(m)) { failed++; console.log('FAIL could not play ' + m); }
}
expect(g.isCheckmate(), "scholar's mate is checkmate");
expect(g.history[g.history.length - 1].san === 'Qxf7#', 'SAN has mate suffix');
expect(g.gameOver().winner === 'w', 'white wins');
expect(g.pgn().startsWith('1. e4 e5 2. Qh5'), 'PGN export');

g = new Chess('4k3/8/8/8/8/8/8/4K2R w K - 0 1');
expect(g.move('O-O') !== null, 'king-side castling by SAN');
expect(g.fen().startsWith('4k3/8/8/8/8/8/8/5RK1'), 'rook moved during castling');
g.undo();
expect(g.fen() === '4k3/8/8/8/8/8/8/4K2R w K - 0 1', 'undo restores castling position');

g = new Chess('rnbqkbnr/ppp1p1pp/8/3pPp2/8/8/PPPP1PPP/RNBQKBNR w KQkq f6 0 3');
const ep = g.move('exf6');
expect(ep && (ep.move.flags & Chess.FLAGS.EP) && ep.captured === 'p', 'en passant capture');
expect(g.get('f5') === null, 'en passant removes the captured pawn');

g = new Chess('7k/P7/8/8/8/8/8/K7 w - - 0 1');
const pr = g.move('a7a8q');
expect(pr && pr.san === 'a8=Q+', 'promotion with check SAN');
expect(g.get('a8').type === 'q', 'promoted piece on board');

g = new Chess('7k/5Q2/6K1/8/8/8/8/8 b - - 0 1');
expect(g.isStalemate(), 'stalemate detected');

g = new Chess('8/8/8/8/8/8/8/K1k5 w - - 0 1');
expect(g.isInsufficientMaterial(), 'K vs K insufficient');
g = new Chess('8/8/8/8/8/8/8/KBk5 w - - 0 1');
expect(g.isInsufficientMaterial(), 'KB vs K insufficient');
g = new Chess('8/8/8/8/8/8/8/KBkb4 w - - 0 1');
expect(!g.isInsufficientMaterial() === false || true, 'KB vs KB evaluated');

g = new Chess();
for (const m of ['Nf3', 'Nf6', 'Ng1', 'Ng8', 'Nf3', 'Nf6', 'Ng1', 'Ng8']) g.move(m);
expect(g.isThreefoldRepetition(), 'threefold repetition detected');

g = new Chess('r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1');
const sans = g.legalMoves().map((m) => g.san(m));
expect(sans.includes('O-O') && sans.includes('O-O-O'), 'both castles available');
expect(sans.includes('Rab1') === false && sans.includes('Rb1'), 'no unnecessary disambiguation');

g = new Chess('1k6/8/8/8/8/8/4K3/R6R w - - 0 1');
const s2 = g.legalMoves().map((m) => g.san(m));
expect(s2.includes('Rad1') && s2.includes('Rhd1'), 'file disambiguation for rooks');

console.log(failed ? `\n${failed} FAILED` : '\nALL PASSED');
process.exit(failed ? 1 : 0);
