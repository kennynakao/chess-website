// Integration checks for the actual app flow with the browser and Pi API stubbed.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const Chess = require('../chess.js');
const elements = new Map();
function node() { return { addEventListener() {}, setAttribute() {}, classList: { add() {}, remove() {}, toggle() {} }, style: {}, dataset: {}, value:'' }; }
const document = { querySelector(s) { if (!elements.has(s)) elements.set(s,node()); return elements.get(s); }, querySelectorAll: () => [], addEventListener() {} };
let handler, uuid=0;
const context = { Chess, document, console, setTimeout: () => 1, clearTimeout() {}, setInterval() {}, performance: { now: () => 0 }, AbortController,
 crypto: { randomUUID: () => 'test-request-'+(++uuid) }, fetch: (...args) => handler(...args) };
vm.createContext(context);
let source = fs.readFileSync(require.resolve('../app.js'),'utf8');
const html = fs.readFileSync(require.resolve('../play.html'),'utf8');
for (const match of source.matchAll(/\$\('#([a-z][a-z0-9-]*)'\)/g)) assert(html.includes('id="'+match[1]+'"'), 'missing element '+match[1]);
source=source.slice(0,source.indexOf('  // ----------------------------------------------------------------- boot'))+`
 renderBoard = renderAll = renderPlayers = renderControls = renderNewGamePanel = renderConnection = hideModal = toast = () => {};
 Sound.play = () => {};
 globalThis.app={Backend,state,applyRemoteState,startGame,requestEngineMove,reconnectPi,canInteract,canPremove,queuePremove,commitMove,takeback};
})();`;
vm.runInContext(source,context);
const a=context.app,b=a.Backend,s=a.state;
function snapshot(moves=[],rev=0,extra={}) { const g=new Chess();moves.forEach(m=>g.move(m));return {ok:true,device:'chessboard',engine:true,levels:[{id:'beginner',label:'Beginner'}],level:'beginner',canControl:true,revision:rev,gameId:'game-1',started:true,fen:g.fen(),uciMoves:moves,gameOver:false,...extra}; }
function response(body,status=200) { return {ok:status<400,status,json:async()=>body}; }
(async()=>{
 a.applyRemoteState(snapshot(['e2e4','e7e5'],2));
 assert.equal(s.game.history.length,2); assert.equal(s.snapshots.length,3);assert.equal(s.mode,'engine');assert.equal(s.tc.base,0);
 assert.equal(b.revision,2);assert.equal(b.gameId,'game-1');assert(a.canInteract());
 console.log('PASS authoritative history hydration restores board, history, revision and unlimited remote play');
 b.accept(snapshot([],0)); a.applyRemoteState(snapshot([],0)); assert.equal(b.revision,2);assert.equal(b.snapshot.uciMoves.length,2);assert.equal(s.game.history.length,2);
 console.log('PASS stale status response cannot roll metadata backward');
 let requests=[];handler=async(path,opts)=>{requests.push(JSON.parse(opts.body));if(requests.length===1)throw new TypeError('network');return response(snapshot(['e2e4','e7e5','g1f3','b8c6'],3));};
 await b.move('g1','f3');assert.equal(requests.length,2);assert.deepEqual(requests[0],requests[1]);assert.equal(requests[0].expectedRevision,2);assert.equal(b.revision,3);assert.equal(b.pending,false);
 console.log('PASS network retry preserves request ID and expected state');
 b.pending=true;assert.equal(a.canInteract(),false);await assert.rejects(b.undo(),/Wait/);b.pending=false;
 handler=async()=>response({ok:false,error:'stale'},409);await assert.rejects(b.undo(),/stale/);assert.equal(b.pending,false);
 console.log('PASS pending mutation blocks input and application conflicts are not blindly retried');
 a.applyRemoteState(snapshot(['e2e4','e7e5'],4));b.available=false;
 await a.startGame('engine');assert.equal(s.mode,'engine');assert.equal(s.game.history.length,2);assert.equal(a.canInteract(),false);
 console.log('PASS offline engine never falls back to a casual bot');
 // Restore fresh state, accept a human move locally, then fail the Pi request.
 a.applyRemoteState(snapshot(['e2e4','e7e5'],4));
 const rec=s.game.move('g1f3');s.snapshots.push({});
 handler=async(path)=>path==='/api/status'?response(snapshot(['e2e4','e7e5'],4)):response({ok:false,error:'engine unavailable'},503);
 await a.requestEngineMove(rec);assert.equal(s.game.history.length,2);assert.equal(s.game.get('g1').type,'n');assert.equal(s.premove,null);assert.equal(s.thinking,false);
 console.log('PASS failed human move reconciles back to Pi-confirmed position');
 handler=async()=>response({ok:false,error:'access code required'},401);
 const probe=await b.probe();assert.equal(probe,null);assert.equal(b.authenticated,false);assert.equal(b.available,false);
 console.log('PASS expired authentication pauses remote play');
 a.applyRemoteState(snapshot([],5,{canControl:false}));assert.equal(a.canInteract(),false);s.game.move('e2e4');assert.equal(a.canPremove(),false);
 console.log('PASS observer session cannot move or queue premoves');
 a.applyRemoteState(snapshot([],6));
 let finishProbe;
 handler=()=>new Promise(resolve=>{finishProbe=resolve;});
 const reconnect=a.reconnectPi({force:true});
 await a.startGame('friend');
 finishProbe(response(snapshot(['e2e4','e7e5'],7)));
 await reconnect;assert.equal(s.mode,'friend');assert.equal(s.game.history.length,0);
 console.log('PASS an in-flight status request cannot replace a newly started local game');
 a.applyRemoteState(snapshot([],8));
 let finishMove, observeSecond;
 const secondSeen=new Promise(resolve=>{observeSecond=resolve;});requests=[];
 handler=async(path,opts)=>{
   requests.push(JSON.parse(opts.body));
   if(requests.length===1)return new Promise(resolve=>{finishMove=resolve;});
   observeSecond();return response(snapshot(['e2e4','e7e5','g1f3','b8c6'],10));
 };
 const first=s.game.move('e2e4');
 const moving=a.requestEngineMove(first);
 assert(a.queuePremove(6,21));
 finishMove(response(snapshot(['e2e4','e7e5'],9)));
 await moving;await secondSeen;
 assert.equal(requests.length,2);assert.equal(requests[1].expectedRevision,9);assert.equal(requests[1].from,'g1');assert.equal(requests[1].to,'f3');
 // Let the second response finish updating presentation state.
 for(let i=0;i<10;i++)await Promise.resolve();
 assert.equal(b.pending,false);assert.equal(s.game.history.length,4);assert.equal(s.premove,null);
 console.log('PASS a queued premove uses the revision returned by the previous Pi move');
 a.applyRemoteState(snapshot([],11,{gameOver:true,outcome:{winner:null,result:'1/2-1/2',reason:'threefold repetition'}}));
 assert.equal(s.status,'over');assert.equal(s.result.reason,'threefold repetition');
 console.log('PASS Pi outcome remains authoritative even when local rules have not ended the game');
})().catch(err=>{console.error(err);process.exitCode=1;});
