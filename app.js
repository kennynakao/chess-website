/* app.js — board UI, clocks, move list, game flow.
 *
 * Rules come from chess.js (client side, for instant hints and highlights).
 * Computer games go through the same-origin /api/* proxy to the Raspberry Pi
 * backend (python-chess + Stockfish).
 * Remote games use the Raspberry Pi as the authoritative game state.
 * When it is unavailable, moves pause; local pass-and-play remains separate.
 * A game on the physical board is two people: the person at the board plays White
 * (their move reaches the Pi from the board's own screen) and this page plays Black.
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

  // ----------------------------------------------------------------- authenticated Raspberry Pi API

  const Backend = {
    available: false, engine: false, authenticated: false, pending: false,
    canControl: false, engineError: null, error: null, levels: [], motion: null,
    revision: 0, gameId: null, snapshot: null, probing: null,
    accept(data) {
      // Revisions only compare within one database: after a fresh install the numbers start again.
      if (this.snapshot && data.serverId === this.snapshot.serverId && data.revision < this.revision) return this.snapshot;
      this.available = true;
      this.authenticated = true;
      this.engine = !!data.engine;
      this.engineError = data.engineError || null;
      this.levels = Array.isArray(data.levels) ? data.levels : [];
      this.canControl = data.canControl === true;
      this.online = data.online !== false;   // the board's screen must have Online selected (older Pis never say)
      this.motion = data.motion || null;
      this.revision = data.revision;
      this.gameId = data.gameId;
      this.snapshot = data;
      this.error = null;
      // Online on the board's screen means the player came to play on the board: that is the default until they choose.
      if (!state.boardPrefStored) state.boardPref = data.online === true && !!this.motion;
      return data;
    },
    async request(path, body, timeout = 12000) {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), timeout);
      try {
        const r = await fetch('/' + path.replace(/^\//, ''), {
          method: body === undefined ? 'GET' : 'POST',
          credentials: 'same-origin', cache: 'no-store', signal: ctrl.signal,
          headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
          body: body === undefined ? undefined : JSON.stringify(body),
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok || data.ok === false) {
          const err = new Error(data.error || (r.status === 401 ? 'Enter your board access code.' : 'The Pi could not complete this request.'));
          err.status = r.status; err.data = data;
          if (r.status === 401) { this.authenticated = false; this.available = false; showAccess(); }
          throw err;
        }
        return data;
      } catch (err) {
        if (!err.status) err.message = 'The Pi is unreachable. Reconnecting…';
        throw err;
      } finally { clearTimeout(timer); }
    },
    async probe() {
      if (this.probing) return this.probing;
      this.probing = (async () => {
        try { return this.accept(await this.request('api/status')); }
        catch (err) { this.available = false; this.error = err.message; return null; }
        finally { this.probing = null; }
      })();
      return this.probing;
    },
    async call(path, body) {
      if (this.pending) throw new Error('Wait for the Pi to finish the current move.');
      if (!this.available || !this.authenticated) throw new Error('Reconnect to the Pi before making a move.');
      const payload = { ...body, requestId: crypto.randomUUID(), expectedRevision: this.revision, expectedGameId: this.gameId };
      this.pending = true;
      renderConnection(); renderControls(); renderNewGamePanel();
      try {
        for (let attempt = 0; attempt < 2; attempt++) {
          try { return this.accept(await this.request(path, payload, 25000)); }
          catch (err) {
            // A retry uses the same request ID and revision; the Pi deduplicates it.
            if (attempt === 0 && (!err.status || [502, 503, 504].includes(err.status))) continue;
            this.error = err.message;
            if (!err.status || err.status >= 500) this.available = false;
            throw err;
          }
        }
      } finally { this.pending = false; renderConnection(); renderControls(); renderNewGamePanel(); }
    },
    // board: true plays on the physical board against the person there (this page is Black; its moves go gantry, camera);
    // false is the digital game against Stockfish.
    newGame(level, board) { return this.call('api/new', { level, board: board === true }); },
    move(from, to, promotion) { return this.call('api/move', { from, to, promotion: promotion || null }); },
    undo() { return this.call('api/undo', {}); },
    // The game ended outside the rules: a clock at zero or a resignation. The Pi records it and resets the board.
    end(reason, loser) { return this.call('api/end', { reason, loser }); },
  };

  // ----------------------------------------------------------------- state

  const state = {
    game: new Chess(),
    status: 'idle',          // idle | playing | over
    mode: 'engine',        // engine (backend Stockfish) | computer (local casual bot) | friend
    board: false,          // this engine game is played out on the physical board (set by the Pi)
    boardColor: null,      // the colour of the person at the physical board, who is the opponent then (set by the Pi)
    boardPref: false,      // what the next engine game should be
    boardPrefStored: false, // the player chose it; otherwise the board is preselected while its screen says Online
    playerColor: 'w',
    colorPref: 'random',
    level: 'beginner',
    tc: TIME_CONTROLS[6],
    clocks: { w: 0, b: 0 },
    running: null,
    lastTick: 0,
    orientation: 'w',
    premove: null,
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
    boardRow: $('#board-row'), boardPick: $('#board-pick'), boardPickNote: $('#board-pick-note'),
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
    connection: $('#connection-status'), reconnect: $('#reconnect-pi'), motionResolve: $('#motion-resolve'), boardConfirm: $('#board-confirm'),
    access: $('#access-overlay'), accessForm: $('#access-form'), accessCode: $('#access-code'), accessError: $('#access-error'), accessSubmit: $('#access-submit'),
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
      // In a game on the board the opponent is the person at the board, not Stockfish.
      const other = state.boardColor
        ? { name: 'Player at the board', cls: state.boardColor === 'w' ? 'white' : 'black', initial: state.boardColor === 'w' ? '♔' : '♚', sub: '' }
        : { name: 'Stockfish', cls: 'bot', initial: '🤖', sub: levelInfo(state.level).label };
      return state.playerColor === 'w' ? { w: you, b: other } : { w: other, b: you };
    }
    if (state.mode === 'computer') {
      const you = { name: 'You', cls: 'you', initial: 'Y', sub: '' };
      const bot = { name: 'Computer', cls: 'bot', initial: '🤖', sub: 'casual' };
      return state.playerColor === 'w' ? { w: you, b: bot } : { w: bot, b: you };
    }
    return { w: { name: 'White', cls: 'white', initial: '♔', sub: '' }, b: { name: 'Black', cls: 'black', initial: '♚', sub: '' } };
  }

  function opponentLabel() {
    if (state.mode === 'engine') {
      if (state.boardColor) return 'vs the player at the board';
      return `vs Stockfish (${levelInfo(state.level).label})${state.board ? ' · on the board' : ''}`;
    }
    if (state.mode === 'computer') return 'vs Computer';
    return 'Pass & Play';
  }

  function tcTitle(tc) {
    return tc.base ? `${tc.label} • ${tc.group}` : 'Unlimited';
  }

  function save() {
    try {
      localStorage.setItem('duen.chess.prefs', JSON.stringify({ tc: state.tc.id, color: state.colorPref, sound: state.sound, level: state.level, pieceStyle: state.pieceStyle,
        ...(state.boardPrefStored ? { board: state.boardPref } : {}) }));
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
      state.boardPrefStored = typeof p.board === 'boolean';
      state.boardPref = p.board === true;
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
  function renderBoard(anim, botMove = false) {
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
      if (live && state.premove && [state.premove.from, state.premove.to].includes(sqi)) cls += ' premove';
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
      if (botMove) d.classList.add('bot-move');
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
    el.board.classList.toggle('grab', canMove || canPremove());
    renderPremove();
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
      const score = r.winner === 'w' ? '1-0' : r.winner === 'b' ? '0-1' : noResult(r) ? '' : '½-½';
      html += `<div class="result-row">${score}<small>${resultText(r)}</small></div>`;
    }
    el.moves.innerHTML = html;
    const cur = $('.mv.cur', el.moves);
    if (cur) cur.scrollIntoView({ block: 'nearest' });
  }

  function reasonText(reason) {
    return { checkmate: 'by checkmate', resignation: 'by resignation', timeout: 'on time', agreement: 'by agreement',
      stalemate: 'by stalemate', 'threefold repetition': 'by repetition', '50-move rule': 'by 50-move rule',
      'insufficient material': 'by insufficient material', 'timeout vs insufficient material': 'by timeout vs insufficient material',
      abandoned: 'abandoned' }[reason] || reason;
  }

  // Aborted here, or ended from the board's screen on the Pi: nobody won and it was not a draw either.
  const noResult = (r) => r.reason === 'aborted' || r.reason === 'abandoned';

  function resultText(r) {
    if (r.reason === 'aborted') return 'Game aborted';
    if (r.reason === 'abandoned') return 'Game abandoned';
    const names = playerNames();
    const who = r.winner ? names[r.winner].name : null;
    return who ? `${who} won ${reasonText(r.reason)}` : `Draw ${reasonText(r.reason)}`;
  }

  function renderControls() {
    const playing = state.status === 'playing';
    const plies = state.game.history.length;
    el.btnDraw.disabled = state.mode === 'engine' || !playing || state.thinking;
    // Resigning a Pi game is reported to the Pi, which records it and puts the pieces back.
    el.btnResign.disabled = !playing || Backend.pending || (state.mode === 'engine' && (!Backend.available || !Backend.canControl));
    el.resignLabel.textContent = playing && plies < 2 && state.mode !== 'engine' ? 'Abort' : 'Resign';
    // No takebacks in a game on the board: the other moves are a person's, made by hand on the board.
    el.btnTakeback.disabled = !playing || plies === 0 || state.thinking || Backend.pending ||
      (state.mode === 'engine' && (!!state.boardColor || !Backend.available || !Backend.canControl || boardBusy()));
    el.btnRematch.disabled = el.btnNewGame.disabled = Backend.pending;
    $('#remote-newgame').disabled = Backend.pending || state.thinking;
    const cur = state.viewPly === null ? plies : state.viewPly;
    el.navFirst.disabled = el.navPrev.disabled = cur === 0;
    el.navNext.disabled = el.navLast.disabled = cur >= plies;
    el.afterGame.classList.toggle('hidden', state.status !== 'over');
    el.gameTitle.textContent = `${tcTitle(state.tc)} • ${opponentLabel()}`;
  }

  function renderAll(anim, botMove = false) {
    renderFrameLabels();
    renderBoard(anim, botMove);
    renderPlayers();
    renderMoves();
    renderControls();
  }

  function renderConnection() {
    const message = !Backend.authenticated ? 'Unlock the Pi to play online' :
      Backend.pending ? 'Pi is processing your move…' :
      !Backend.available ? 'Pi offline — reconnecting. Your game is saved.' :
      !Backend.engine && stockfishNeeded() ? 'Pi connected — Stockfish unavailable' :
      !Backend.canControl ? 'Watching the Pi · another player has control' :
      stockfishNeeded() ? 'Connected to Raspberry Pi · Stockfish ready' : 'Connected to Raspberry Pi';
    el.connection.textContent = Backend.available && motionText() ? `${message} · ${motionText()}` : message;
    el.reconnect.textContent = Backend.authenticated ? 'Reconnect' : 'Unlock';
    const motion = Backend.motion || {};
    const waiting = motion.state === 'waiting';
    el.motionResolve.classList.toggle('hidden', !((boardFaulted() || waiting && motion.mismatch) && Backend.canControl));
    el.boardConfirm.classList.toggle('hidden', !(waiting && Backend.canControl));
    el.boardConfirm.disabled = false;
  }

  // Only the digital game has Stockfish in it; a game on the board is two people. With no Pi game on, it is the next game's choice.
  const stockfishNeeded = () => (state.mode === 'engine' && state.status !== 'idle' ? !state.boardColor : !state.boardPref);

  // The physical board stopped. That concerns the hardware, whatever game is on screen.
  const boardFaulted = () => !!Backend.motion && Backend.motion.state === 'fault';
  // In a board game the player's move runs gantry, then camera, and only then is it the turn of the person at the board.
  // A digital game never waits for the board.
  const boardBusy = () => state.board && !!Backend.motion && ['moving', 'waiting', 'fault'].includes(Backend.motion.state);
  // The person at the board is thinking: their move reaches the Pi when they tap Done on the board's screen.
  const boardPlayerToMove = () => state.mode === 'engine' && state.status === 'playing' && !!state.boardColor &&
    state.game.turn === state.boardColor && !boardBusy();

  let followTimer = null, followingMyMove = false;
  // A person can think for minutes, so their move is looked for less often than the gantry is followed.
  const BOARD_PLAYER_POLL_MS = 2000;
  function followBoard(delay = 700) {
    if (followTimer) return;
    followTimer = setTimeout(() => { followTimer = null; followBoardOnce(); }, delay);
  }

  /** Catch up with the board: the gantry and camera finish the player's move, then the other player's move appears soon after it is made. */
  async function followBoardOnce() {
    const before = state.game.history.length, wasPlaying = state.status === 'playing';
    await reconnectPi();
    if (state.mode !== 'engine') return;
    if (followingMyMove && wasPlaying && state.status === 'over') announceResult();
    else if (state.game.history.length > before) Sound.play(Backend.snapshot && Backend.snapshot.check ? 'check' : 'move');
    if (Backend.available && boardBusy()) { followBoard(); return; }
    if (Backend.available && boardPlayerToMove()) { followBoard(BOARD_PLAYER_POLL_MS); return; }
    followingMyMove = false;
    playPremove();
  }

  /** What the physical board is doing; empty when there is nothing to say, and silent during a digital game. */
  function motionText() {
    const motion = Backend.motion;
    if (!motion) return '';
    if (motion.state === 'fault') {
      if (!Backend.canControl) return 'Board stopped: the player is fixing it';
      return `Board stopped: ${motion.error ? `${motion.error} ` : ''}Set the pieces to match the screen, then confirm`;
    }
    if (!state.board) return '';
    if (motion.state === 'waiting') {
      const queued = motion.pending ? ` (+${motion.pending} queued)` : '';
      return [`On the board: ${(motion.instructions || []).join(' ')}${queued}`, boardProblemText(motion.mismatch)].filter(Boolean).join(' ');
    }
    if (motion.state === 'moving') {
      const connected = motion.mode === 'simulated' ? ' (not connected)' : '';
      // After a game ends the board docks and carries every piece home, and the camera checks the start.
      if (motion.current === 'reset') return RESETTING;
      if (motion.stage === 'camera') return `Camera checking ${motion.current}`;
      return `Gantry moving ${motion.current}${connected}`;
    }
    if (boardPlayerToMove()) return 'Waiting for the player at the board';
    if (motion.mode === 'simulated') return motion.camera ? 'Gantry not connected' : 'Gantry and camera not connected';
    if (motion.mode === 'arduino') return 'Gantry not set up on the Pi';
    return motion.mode === 'manual' ? 'Moving pieces by hand' : 'Board ready';
  }
  const RESETTING = 'The board is putting the pieces back…';
  const boardResetting = () => !!Backend.motion && Backend.motion.state === 'moving' && Backend.motion.current === 'reset';

  /** The state of the hardware, for the "On the board" choice before a game starts. */
  function boardOptionNote(motion) {
    if (!motion) return 'Pieces move on the physical board';
    if (motion.mode === 'simulated') return motion.camera ? 'Gantry not connected yet' : 'Gantry and camera not connected yet';
    if (motion.mode === 'arduino') return 'Gantry not set up on the Pi yet';
    if (motion.mode === 'manual') return 'You move the pieces by hand';
    return motion.camera ? 'Gantry moves, camera checks' : 'Gantry moves the pieces';
  }

  /** What the camera saw wrong, in plain words. */
  function boardProblemText(problem) {
    if (!problem) return '';
    if (problem.error) return problem.error;
    if ((problem.uncertain || []).length) return `The camera can't tell about ${problem.uncertain.join(', ')}. Try again.`;
    const parts = [];
    if ((problem.missing || []).length) parts.push(`no piece on ${problem.missing.join(', ')}`);
    if ((problem.extra || []).length) parts.push(`an extra piece on ${problem.extra.join(', ')}`);
    return `The camera sees ${parts.join(' and ')}.`;
  }

  /** Tell the Pi the step on screen is done. Names that step, so a double press can't confirm the next one. */
  async function confirmBoard(override = false) {
    el.boardConfirm.disabled = true;
    const jobId = Backend.motion && Backend.motion.jobId;
    try { await Backend.request('api/board-confirm', { ...(override ? { override: true } : {}), ...(jobId ? { jobId } : {}) }); }
    catch (err) { toast(err.message, 4500); }
    // Give the Pi a moment to move on. Unforced, so this never replaces a game or panel on screen.
    setTimeout(() => reconnectPi(), 300);
  }

  /** The time control menu, built once: the panel is re-rendered on every Pi poll and must not rebuild it under the pointer. */
  function buildTcMenu() {
    const groups = [];
    for (const tc of TIME_CONTROLS) {
      let g = groups.find((x) => x.name === tc.group);
      if (!g) groups.push((g = { name: tc.group, items: [] }));
      g.items.push(tc);
    }
    el.tcMenu.innerHTML = groups.map((g) =>
      `<div class="tc-group"><div class="tc-group-name">${GROUP_ICON[g.name] || ''} ${g.name}</div><div class="tc-grid">` +
      g.items.map((tc) => `<button type="button" class="tc-opt" data-tc="${tc.id}">${tc.label}</button>`).join('') +
      '</div></div>').join('');
  }

  function renderNewGamePanel() {
    // The clocks are this browser's, so the time control is the player's choice in every game, Pi games included.
    el.tcLabel.textContent = state.tc.label;
    $$('.tc-opt', el.tcMenu).forEach((button) => button.classList.toggle('on', button.dataset.tc === state.tc.id));
    el.colorRow.classList.add('hidden');
    // The choice between the digital game and the board game, once the Pi has said what hardware it has.
    const hardware = Backend.available ? Backend.motion : null;
    const onBoard = !!hardware && state.boardPref;
    // A game on the board is against the person at the board: no Stockfish, so no level to pick.
    el.levelRow.classList.toggle('hidden', onBoard);
    if (Backend.levels.length) {
      if (!Backend.levels.some(l => l.id === state.level)) state.level = Backend.levels[0].id;
      el.levelPick.innerHTML = Backend.levels.map(l =>
        `<button type="button" class="${l.id === state.level ? 'on' : ''}" data-level="${l.id}">${l.label}<small>${l.description || ''}</small></button>`).join('');
    }
    el.boardRow.classList.toggle('hidden', !hardware);
    if (hardware) {
      $$('button', el.boardPick).forEach((button) => button.classList.toggle('on', (button.dataset.board === '1') === state.boardPref));
      el.boardPickNote.textContent = boardOptionNote(hardware);
    }
    el.playBotTitle.textContent = 'Play on the Pi';
    const timing = state.tc.base ? state.tc.label : 'unlimited time';
    el.playBotSub.textContent = onBoard ? `You play Black against the player at the board · ${timing}` : `You play White · ${timing}`;
    el.playBtn.textContent = Backend.pending ? 'Please wait…' : 'Play on the Pi';
    // A board game cannot start while the board is still putting the last game's pieces back.
    const resetting = state.boardPref && boardResetting();
    const engineMissing = !Backend.engine && !state.boardPref;   // only the digital game needs Stockfish
    el.playBtn.disabled = el.playBot.disabled = Backend.pending || (Backend.authenticated && (!Backend.available || engineMissing || !Backend.canControl || !Backend.online || resetting));
    el.playFriend.disabled = Backend.pending;
    el.backendStatus.textContent = !Backend.authenticated ? 'Use your board access code to connect.' :
      !Backend.available ? (Backend.error || 'The Pi is offline. Reconnect when it is online.') :
      !Backend.online ? 'Tap Online on the board’s screen to connect this website to it.' :
      !Backend.canControl ? 'Another player is using this board. You can watch their game.' :
      resetting ? RESETTING :
      engineMissing ? (Backend.engineError || 'Stockfish is not ready on the Pi.') :
      onBoard ? 'The player at the board plays White; your moves are carried out on the board.' :
      'Your moves and the computer’s replies are processed on the Raspberry Pi.';
    el.backendStatus.className = 'backend-status ' + (Backend.available && !engineMissing && Backend.online ? 'ok' : 'warn');
    el.soundToggle.classList.toggle('off', !state.sound);
    renderConnection();
  }

  function showAccess() {
    if (state.mode === 'friend' && state.status === 'playing') return;
    el.access.classList.remove('hidden');
  }

  let orientedGame = null;   // the Pi game the board was last turned to its player for
  function applyRemoteState(data, { preservePremove = false } = {}) {
    // A slower response must never render an older position with a newer revision.
    data = Backend.accept(data);
    // Reconstruct every ply: FEN alone cannot restore repetition or takeback history.
    const game = new Chess();
    const records = [];
    for (const move of data.uciMoves || []) {
      const record = game.move(move);
      if (!record) throw new Error('The Pi returned an invalid move history. Reconnect to reload it.');
      records.push(record);
    }
    if (data.fen && !sameBoard(game.fen(), data.fen)) throw new Error('The Pi position could not be loaded. Reconnect to try again.');
    const premove = preservePremove ? state.premove : null;
    const previousMoves = state.game.history.map((h) => h.uci), previousPlies = previousMoves.length;
    const sameGame = !!clockGame && clockGame.gameId === data.gameId && clockGame.serverId === data.serverId;
    clearTimeout(state.botTimer);
    state.game = new Chess(); state.snapshots = []; pushSnapshot(null);
    for (const rec of records) pushSnapshot(state.game.move(rec.move));
    state.gameId++;
    // The Pi says which colour this page plays: Black in a game on the board, where the person at the board is White.
    state.mode = 'engine'; state.playerColor = data.playerColor === 'b' ? 'b' : 'w'; state.board = data.board === true;
    state.boardColor = data.boardColor === 'w' || data.boardColor === 'b' ? data.boardColor : null;
    // A new or newly loaded game is seen from its player's side; a flip within the game stays until the next one.
    if (data.started && orientedGame !== data.serverId + ' ' + data.gameId) {
      orientedGame = data.serverId + ' ' + data.gameId;
      state.orientation = state.playerColor;
    }
    // The move made at the board arrives with a poll and slides in, as a reply played here would.
    const last = records[records.length - 1];
    const arrived = sameGame && !!state.boardColor && !!last && last.color === state.boardColor &&
      records.length === previousPlies + 1 && previousMoves.every((uci, i) => records[i].uci === uci);
    state.level = data.level || state.level;
    state.selected = null; state.legal = []; state.pendingPromo = null; state.drag = null; state.hoverSq = null;
    state.premove = premove; state.viewPly = null; state.thinking = false;
    state.result = data.started && data.gameOver ? (data.outcome || state.game.gameOver() ||
      { winner: null, result: '1/2-1/2', reason: 'confirmed by the Pi' }) : null;
    state.status = !data.started ? 'idle' : data.gameOver ? 'over' : 'playing';
    if (state.status === 'over') state.premove = null;
    syncClocks(data, previousPlies);
    el.promo.classList.add('hidden'); el.confirm.classList.add('hidden');
    el.board.classList.remove('dragging'); hideModal();
    el.panelNew.classList.toggle('hidden', state.status !== 'idle');
    el.panelGame.classList.toggle('hidden', state.status === 'idle');
    renderNewGamePanel(); renderAll(arrived ? moveAnimation(last.move) : undefined, arrived);
  }

  async function reconnectPi({ force = false } = {}) {
    if (Backend.pending || state.thinking || (!force && state.mode === 'friend' && state.status === 'playing')) return;
    const previousGame = Backend.gameId, previousRevision = Backend.revision, generation = state.gameId;
    const data = await Backend.probe();
    if (data && generation === state.gameId && !Backend.pending && !state.thinking && (force || state.mode === 'engine' &&
        (previousGame !== data.gameId || previousRevision !== data.revision || !sameBoard(state.game.fen(), data.fen)))) {
      // A routine poll keeps a queued premove: it may be for the Stockfish reply this very poll brings.
      try { applyRemoteState(data, { preservePremove: !force }); } catch (err) { Backend.available = false; Backend.error = err.message; toast(err.message, 4500); }
    }
    renderNewGamePanel(); renderControls();
  }

  // ----------------------------------------------------------------- game flow

  function resetGameState() {
    state.premove = null;
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
    clockGame = null;   // a local game: the Pi game's clocks stay saved for when the tab comes back to it
    el.promo.classList.add('hidden');
    el.confirm.classList.add('hidden');
    hideModal();
  }

  async function startGame(mode, colorPref) {
    if (Backend.pending || state.thinking) return;
    if (mode !== 'friend') {
      if (!Backend.authenticated) { showAccess(); return; }
      // Only the digital game needs Stockfish; a game on the board is against the person there.
      if (!Backend.available || (!Backend.engine && !state.boardPref) || !Backend.canControl) { toast('The Pi is not ready for a new game. Reconnect to check.'); return; }
      try { applyRemoteState(await Backend.newGame(state.level, state.boardPref)); Sound.play('start'); }
      catch (err) { await reconnectPi({ force: true }); toast(err.message, 4500); }
      return;
    }
    resetGameState();
    state.mode = 'friend'; state.status = 'playing'; state.playerColor = 'w'; state.orientation = 'w'; state.boardColor = null;
    orientedGame = null;   // back to the Pi game, the board turns to its player again
    el.access.classList.add('hidden');
    el.panelNew.classList.add('hidden'); el.panelGame.classList.remove('hidden');
    renderAll(); Sound.play('start');
  }

  function canPremove() {
    return state.status === 'playing' && state.mode !== 'friend' &&
      (state.mode !== 'engine' || Backend.available && Backend.canControl && !(state.board && boardFaulted())) &&
      state.game.turn !== state.playerColor && !state.pendingPromo && state.viewPly === null;
  }

  function renderPremove() {
    const overlay = $('#premove-arrow');
    const move = state.viewPly === null ? state.premove : null;
    overlay.innerHTML = '';
    if (!move) return;
    const [x1, y1] = toXY(move.from), [x2, y2] = toXY(move.to);
    overlay.innerHTML = `<defs><marker id="premove-head" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="3" markerHeight="3" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#4cb0ff"/></marker></defs><line x1="${x1 + .5}" y1="${y1 + .5}" x2="${x2 + .5}" y2="${y2 + .5}" stroke="#4cb0ff" stroke-width=".17" stroke-linecap="round" opacity=".9" marker-end="url(#premove-head)"/>`;
  }

  function clearPremove() {
    state.premove = null;
    state.selected = null;
    state.legal = [];
    renderBoard();
  }

  function queuePremove(from, to) {
    if (!canPremove() || from === to) return false;
    const piece = state.game.board[from];
    if (!piece || colorOf(piece) !== state.playerColor) return false;
    state.premove = { from, to };
    state.selected = null;
    state.legal = [];
    renderBoard();
    toast('Premove queued · right-click or Esc to cancel');
    return true;
  }

  function playPremove() {
    if (!state.premove || !canInteract() || state.viewPly !== null) return;
    const move = state.premove;
    state.premove = null;
    // Validate against the actual position after the opponent's reply.
    if (!tryMove(move.from, move.to, { animate: true, source: 'human' })) {
      renderBoard();
      toast('Premove cancelled — no longer legal');
    }
  }

  function canInteract() {
    return state.status === 'playing' && !state.pendingPromo && !state.thinking &&
      (state.mode !== 'engine' || Backend.available && Backend.canControl && !Backend.pending && !boardBusy()) &&
      (state.mode === 'friend' || state.game.turn === state.playerColor);
  }

  function select(sqi) {
    state.selected = sqi;
    state.legal = canPremove() ? [] : state.game.movesFrom(sqi);
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
    if (canPremove()) return queuePremove(from, to);
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
    state.drag = null;
    state.hoverSq = null;
    el.board.classList.remove('dragging');
    pushSnapshot(rec);

    if (state.tc.base) {
      if (state.running) tick();
      state.clocks[mover] += state.tc.inc * 1000;
      state.running = g.turn;
      state.lastTick = performance.now();
      saveClocks();
    }

    state.selected = null;
    state.legal = [];
    state.viewPly = null;
    state.pendingPromo = null;
    el.promo.classList.add('hidden');
    el.confirm.classList.add('hidden');

    renderAll(opts.animate !== false ? moveAnimation(m) : null, opts.source === 'engine' || opts.source === 'bot');

    const over = g.gameOver();
    if (over && !(state.mode === 'engine' && opts.source !== 'engine')) {
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

  /** Where each moving piece slides from, for renderBoard: the piece itself, and the rook when castling. */
  function moveAnimation(m) {
    const anim = { [m.to]: m.from };
    if (m.flags & FLAGS.CASTLE_K) anim[m.to - 1] = m.to + 1;
    if (m.flags & FLAGS.CASTLE_Q) anim[m.to + 1] = m.to - 2;
    return anim;
  }

  // Run the engine and this presentation delay together so engine time is included.
  const botThinkMilliseconds = () => 1000 + Math.random() * 2000;

  /** Send the player's move to the Pi. In a digital game Stockfish's reply comes back with it; in a board game the
   *  screen follows the gantry and camera, then waits for the move of the person at the board. */
  async function requestEngineMove(rec) {
    state.thinking = true; renderPlayers(); renderControls();
    try {
      const res = await Backend.move(sqName(rec.move.from), sqName(rec.move.to), rec.move.promo || null);
      applyRemoteState(res, { preservePremove: true });
      if (state.status === 'over') announceResult();
      else Sound.play(res.check ? 'check' : 'move');
      // Metadata is updated before a queued premove starts the next request.
      playPremove();
      followingMyMove = true;
      if (boardBusy()) followBoard();
    } catch (err) {
      state.thinking = false; state.premove = null;
      // Restore the last confirmed position while status reconciliation runs.
      const authenticated = Backend.authenticated;
      if (Backend.snapshot) applyRemoteState(Backend.snapshot);
      Backend.authenticated = authenticated; Backend.available = false;
      await reconnectPi({ force: true });
      renderPlayers(); renderControls();
      // The Pi may have committed a game-ending move even though its reply was lost.
      if (state.status === 'over') announceResult();
      else toast(err.message, 5000);
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
    state.thinking = true;
    renderPlayers();
    renderControls();
    state.botTimer = setTimeout(() => {
      if (id !== state.gameId || state.status !== 'playing') return;
      const g = state.game;
      const moves = g.legalMoves();
      if (!moves.length) {
        state.thinking = false;
        renderPlayers();
        renderControls();
        return;
      }
      const caps = moves.filter((m) => m.flags & FLAGS.CAPTURE);
      const pool = caps.length && Math.random() < 0.65 ? caps : moves;
      let m = pool[Math.floor(Math.random() * pool.length)];
      if (m.promo) m = pool.find((x) => x.from === m.from && x.to === m.to && x.promo === 'q') || m;
      state.thinking = false;
      commitMove(m, { animate: true, source: 'bot' });
      playPremove();
    }, botThinkMilliseconds());
  }

  function endGame(winner, reason) {
    state.premove = null;
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
    if (reason !== 'aborted') announceResult();
  }

  /** Result sound and popup, only for the player who just finished the game (not viewers or reloads). */
  function announceResult() {
    const youWon = state.mode !== 'friend' && state.result.winner === state.playerColor;
    Sound.play(youWon ? 'win' : 'end');
    showModal();
  }

  function showModal() {
    const r = state.result;
    const names = playerNames();
    let title, cls = '';
    if (r.reason === 'abandoned') { title = 'Game abandoned'; cls = 'draw'; }
    else if (state.mode !== 'friend') {
      if (r.winner === state.playerColor) title = 'You Won!';
      else if (r.winner) { title = 'You Lost'; cls = 'lose'; }
      else { title = 'Draw'; cls = 'draw'; }
    } else {
      title = r.winner ? `${names[r.winner].name} Won!` : 'Draw';
      if (!r.winner) cls = 'draw';
    }
    el.modalTitle.textContent = title;
    el.modalSub.textContent = r.reason === 'abandoned' ? 'ended from the board' : reasonText(r.reason);
    el.modalHeader.className = 'modal-header ' + cls;
    el.modalScore.textContent = r.winner === 'w' ? '1 - 0' : r.winner === 'b' ? '0 - 1' : noResult(r) ? '—' : '½ - ½';
    el.modalNameW.textContent = names.w.name;
    el.modalNameB.textContent = names.b.name;
    el.modalAvW.className = 'avatar ' + names.w.cls; el.modalAvW.textContent = names.w.initial;
    el.modalAvB.className = 'avatar ' + names.b.cls; el.modalAvB.textContent = names.b.initial;
    el.modal.classList.remove('hidden');
  }
  function hideModal() { el.modal.classList.add('hidden'); }

  function resignOrAbort() {
    if (state.status !== 'playing') return;
    if (state.mode === 'engine') return reportEnd('resignation', state.playerColor);
    if (state.game.history.length < 2) { endGame(null, 'aborted'); toast('Game aborted'); return; }
    const loser = state.mode === 'friend' ? state.game.turn : state.playerColor;
    endGame(opp(loser), 'resignation');
  }

  let endReported = null;  // the Pi game whose end this tab has sent, so a clock at zero reports it once
  /** Tell the Pi a game ended outside the rules (a clock at zero, a resignation). Its snapshot shows the result. */
  async function reportEnd(reason, loser) {
    if (state.mode !== 'engine' || state.status !== 'playing' || !Backend.canControl || endReported === Backend.gameId) return;
    endReported = Backend.gameId;
    state.running = null;
    try {
      applyRemoteState(await Backend.end(reason, loser));
      if (state.status === 'over') announceResult();
    } catch (err) {
      await reconnectPi({ force: true });
      // The Pi may have recorded the end even though its reply was lost.
      if (state.status === 'over') announceResult();
      else toast(err.message, 5000);
    }
  }

  function offerDraw() {
    if (state.status !== 'playing' || state.mode === 'engine') return;
    if (state.mode === 'friend') { endGame(null, 'agreement'); return; }
    const bal = state.game.materialBalance();
    const botAhead = state.playerColor === 'w' ? bal < 0 : bal > 0;
    if (!botAhead) endGame(null, 'agreement');
    else toast((state.mode === 'engine' ? 'Stockfish' : 'Computer') + ' declined the draw offer');
  }

  async function takeback() {
    if (state.status !== 'playing' || !state.game.history.length || state.thinking || Backend.pending) return;
    if (state.mode === 'engine') {
      if (state.boardColor) { toast('Takebacks are off in a game on the board.'); return; }
      try { applyRemoteState(await Backend.undo()); }
      catch (err) { await reconnectPi({ force: true }); toast('Takeback failed: ' + err.message, 4500); }
      return;
    }
    state.game.undo(); state.snapshots.pop();
    state.premove = null; state.selected = null; state.legal = []; state.viewPly = null; state.pendingPromo = null;
    el.promo.classList.add('hidden'); renderAll();
  }

  function setViewPly(n) {
    state.premove = null;
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

  // The clocks are this browser's: the Pi knows nothing about time. They belong to one game at a time and are kept in
  // localStorage, so a reloaded tab (or one back from a Pass & Play game) carries a Pi game's clocks on where they were.
  const CLOCKS_KEY = 'duen.chess.clocks';
  let clockGame = null;   // { serverId, gameId } of the Pi game whose clocks state.clocks hold; null during a local game
  let clocksSaved = 0;

  function saveClocks() {
    if (!clockGame || !state.tc.base) return;
    clocksSaved = performance.now();
    try { localStorage.setItem(CLOCKS_KEY, JSON.stringify({ ...clockGame, tc: state.tc.id, clocks: state.clocks })); } catch (e) { /* ignore */ }
  }

  /** Keep the clocks in step with a Pi snapshot: a new game gets new clocks; the same game gives each new mover the increment. */
  function syncClocks(data, previousPlies) {
    const plies = state.game.history.length;
    if (!clockGame || clockGame.gameId !== data.gameId || clockGame.serverId !== data.serverId) {
      clockGame = { serverId: data.serverId, gameId: data.gameId };
      let saved = null;
      try { saved = JSON.parse(localStorage.getItem(CLOCKS_KEY) || 'null'); } catch (e) { /* ignore */ }
      if (data.started && saved && saved.gameId === data.gameId && saved.serverId === data.serverId && saved.clocks) {
        state.tc = TIME_CONTROLS.find((t) => t.id === saved.tc) || state.tc;
        state.clocks = { w: Math.max(0, +saved.clocks.w || 0), b: Math.max(0, +saved.clocks.b || 0) };
      } else {
        state.clocks = { w: state.tc.base * 1000, b: state.tc.base * 1000 };
      }
    } else if (state.tc.base) {
      for (let ply = previousPlies; ply < plies; ply++) state.clocks[ply % 2 ? 'b' : 'w'] += state.tc.inc * 1000;
    }
    // As in a local game, the clocks start with White's first move.
    state.running = state.status === 'playing' && plies ? state.game.turn : null;
    state.lastTick = performance.now();
    saveClocks();
  }

  function tick() {
    if (state.status !== 'playing' || !state.tc.base) return;
    const now = performance.now();
    // A viewer's copy of the clocks stands still. In a board game nobody is thinking while the gantry carries a piece
    // or the camera checks it, so the clocks wait with the board; White's runs while the person at the board thinks.
    const paused = state.mode === 'engine' && (!Backend.canControl || boardBusy());
    if (state.running && !paused) state.clocks[state.running] -= now - state.lastTick;
    state.lastTick = now;
    // Either clock may be the one at zero: the opponent's can run out while they think.
    const loser = ['w', 'b'].find((color) => state.clocks[color] <= 0);
    if (!loser) {
      renderClocks();
      if (clockGame && now - clocksSaved >= 1000) saveClocks();
      return;
    }
    state.clocks[loser] = 0;
    renderClocks();
    if (state.mode === 'engine') {
      // The Pi records the result and, in a board game, puts the pieces back. Not while a move is still with it.
      if (!Backend.pending && !state.thinking) reportEnd('timeout', loser);
      return;
    }
    const winner = opp(loser);
    if (state.game.hasMatingMaterial(winner)) endGame(winner, 'timeout');
    else endGame(null, 'timeout vs insufficient material');
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
    if (!canInteract() && !canPremove()) return;
    const sqi = squareFromEvent(e);
    if (sqi < 0) return;
    const p = state.game.board[sqi];

    if (state.selected !== null && (state.legal.some((m) => m.to === sqi) ||
        (canPremove() && sqi !== state.selected && (!p || colorOf(p) !== state.playerColor)))) {
      tryMove(state.selected, sqi, { animate: true, source: 'human' });
      return;
    }
    if (p && colorOf(p) === (canPremove() ? state.playerColor : state.game.turn)) {
      const wasSelected = state.selected === sqi;
      state.premove = null;
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
    if (d.moved && target >= 0 && target !== d.sqi && (canPremove() || state.legal.some((m) => m.to === target))) {
      tryMove(d.sqi, target, { animate: false, source: 'human' });
    } else if (!d.moved && d.wasSelected) {
      deselect();
    } else {
      renderBoard(); // snap back
    }
  }
  el.board.addEventListener('contextmenu', (e) => { e.preventDefault(); clearPremove(); });
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
    renderClocks();
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
  el.boardPick.addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    state.boardPref = b.dataset.board === '1';
    state.boardPrefStored = true;
    save();
    renderNewGamePanel();
  });
  const startComputerGame = () => startGame('engine');
  el.playBtn.addEventListener('click', startComputerGame);
  el.playBot.addEventListener('click', startComputerGame);
  el.playFriend.addEventListener('click', () => startGame('friend'));

  el.btnResign.addEventListener('click', () => {
    if (state.status !== 'playing') return;
    if (state.game.history.length < 2 && state.mode !== 'engine') { resignOrAbort(); return; }
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
    if (Backend.pending || state.thinking) return;
    state.premove = null;
    clearTimeout(state.botTimer);
    state.gameId++;
    state.status = 'idle';
    state.running = null;
    state.thinking = false;
    hideModal();
    renderBoard();
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
      Result: r ? (r.winner === 'w' ? '1-0' : r.winner === 'b' ? '0-1' : noResult(r) ? '*' : '1/2-1/2') : '*',
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
      case 'Escape': clearPremove(); hideModal(); el.confirm.classList.add('hidden'); if (state.pendingPromo) { state.pendingPromo = null; el.promo.classList.add('hidden'); deselect(); renderBoard(); } break;
    }
  });

  document.addEventListener('click', (e) => {
    if (!e.target.closest('.tc-picker')) {
      el.tcMenu.classList.add('hidden');
      el.tcButton.setAttribute('aria-expanded', 'false');
    }
  });

  el.accessForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    el.accessSubmit.disabled = true; el.accessError.textContent = '';
    try {
      await Backend.request('api/login', { code: el.accessCode.value });
      Backend.authenticated = true; el.accessCode.value = ''; el.access.classList.add('hidden');
      await reconnectPi({ force: true });
    } catch (err) { el.accessError.textContent = err.message; }
    finally { el.accessSubmit.disabled = false; }
  });
  $('#access-local').addEventListener('click', () => startGame('friend'));
  el.reconnect.addEventListener('click', () => {
    if (!Backend.authenticated) showAccess(); else reconnectPi({ force: true });
  });
  $('#remote-newgame').addEventListener('click', newGame);
  el.motionResolve.addEventListener('click', async () => {
    if (!boardFaulted()) { await confirmBoard(true); return; }
    try { await Backend.request('api/motion-resolve', {}); await reconnectPi(); }
    catch (err) { toast(err.message, 4500); }
  });
  el.boardConfirm.addEventListener('click', () => confirmBoard());

  // ----------------------------------------------------------------- boot

  restore();
  applyPieceStyle();
  buildSquares();
  buildTcMenu();
  pushSnapshot(null);
  renderNewGamePanel();
  renderAll();
  reconnectPi({ force: true });
  setInterval(async () => {
    if (document.hidden) return;
    await reconnectPi();
    if (boardBusy()) followBoard(); else if (boardPlayerToMove()) followBoard(BOARD_PLAYER_POLL_MS);
  }, 5000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) reconnectPi(); });
})();
