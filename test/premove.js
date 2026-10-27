// Exercise the app's premove flow with DOM/audio rendering stubbed out.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const Chess = require('../chess.js');
const node = { addEventListener() {}, classList: { add() {}, remove() {}, toggle() {} }, style: {}, dataset: {} };
const document = { querySelector: () => node, querySelectorAll: () => [], addEventListener() {} };
const context = { Chess, document, console, setTimeout: () => 1, clearTimeout() {}, setInterval() {}, performance: { now: () => 0 } };
vm.createContext(context);
let source = fs.readFileSync(require.resolve('../app.js'), 'utf8');
source = source.slice(0, source.indexOf('  // ----------------------------------------------------------------- boot')) + `
  renderBoard = renderAll = renderPlayers = renderControls = hideModal = toast = () => {};
  scheduleBot = requestEngineMove = () => {};
  Sound.play = () => {};
  globalThis.testApp = { state, queuePremove, playPremove, clearPremove, commitMove,
    resetGameState, setViewPly, endGame, renderPremove, canPremove };
})();`;
vm.runInContext(source, context);
const a = context.testApp, s = a.state;
function setup(fen, color = 'w') {
  a.resetGameState();
  s.game = new Chess(fen || Chess.START_FEN);
  s.status = 'playing'; s.mode = 'computer'; s.playerColor = color;
  s.tc = { base: 0 }; s.orientation = 'w'; s.snapshots = [];
}
setup();
a.commitMove('e2e4', {source:'human'});
assert(a.queuePremove(6,21)); // g1-f3
const before = s.game.fen();
a.playPremove();
assert.equal(s.game.fen(), before, 'must wait for opponent');
a.renderPremove();
assert.match(node.innerHTML, /marker-end/);
a.commitMove('e7e5', {source:'bot'}); a.playPremove();
assert.equal(s.game.history.length, 3);
assert.equal(s.game.get('f3').type, 'n');
assert.equal(s.premove, null);
console.log('PASS legal premove waits, draws arrow, executes once');
setup(); a.commitMove('e2e4', {source:'human'});
a.queuePremove(0, 56); a.commitMove('e7e5', {source:'bot'});
const illegalBefore = s.game.fen(); a.playPremove();
assert.equal(s.game.fen(), illegalBefore); assert.equal(s.premove, null);
console.log('PASS illegal premove discarded');
setup('4k3/8/8/8/8/8/1r6/R3K3 b Q - 0 1');
a.queuePremove(0,8); a.commitMove('b2a2', {source:'bot'}); a.playPremove();
assert.equal(s.game.get('a2').type, 'r');
assert.equal(s.game.get('a2').color, 'w');
console.log('PASS premove revalidated against changed position');
setup(undefined, 'b');
assert(a.queuePremove(52,36));
a.commitMove('e2e4', {source:'human'}); a.playPremove();
assert.equal(s.game.get('e5').color, 'b');
console.log('PASS black premove');
setup(); a.commitMove('e2e4', {source:'human'}); a.queuePremove(6,21);
a.clearPremove(); assert.equal(s.premove,null);
a.queuePremove(6,21); a.resetGameState(); assert.equal(s.premove,null);
s.mode='friend'; assert.equal(a.canPremove(),false);
console.log('PASS cancellation, reset, and pass-and-play exclusion');
