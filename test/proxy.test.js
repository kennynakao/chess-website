'use strict';

const { test, beforeEach, afterEach } = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const handler = require('../api/[action].js');

const ENV = {
  CHESS_SESSION_SECRET: 'unit-test-session-secret-not-a-real-credential',
  CHESS_ACCESS_CODE: 'unit-test-access-code',
  CHESS_RELAY_TOKEN: 'unit-test-relay-secret-not-a-real-credential',
  PI_API_URL: 'https://pi.example.invalid',
};
const originalEnv = Object.fromEntries(Object.keys(ENV).map(key => [key, process.env[key]]));
const originalFetch = global.fetch;
let calls;

beforeEach(() => {
  Object.assign(process.env, ENV);
  calls = [];
  global.fetch = async (...args) => {
    calls.push(args);
    throw new Error('Unexpected network access in unit test');
  };
});
afterEach(() => {
  global.fetch = originalFetch;
  for (const [key, value] of Object.entries(originalEnv)) {
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
  }
});

async function request(action, { method = 'POST', body = {}, headers = {} } = {}) {
  const req = {
    url: '/api/' + action,
    method,
    body,
    headers: {
      host: 'chess.example.invalid',
      origin: 'https://chess.example.invalid',
      'content-type': 'application/json',
      ...headers,
    },
  };
  const result = { status: null, headers: {}, body: null, raw: null };
  const res = {
    setHeader(key, value) { result.headers[key.toLowerCase()] = value; },
    set statusCode(value) { result.status = value; },
    end(value) { result.raw = value; result.body = JSON.parse(value); },
  };
  await handler(req, res);
  return result;
}

async function login(headers = {}) {
  const reply = await request('login', { body: { code: ENV.CHESS_ACCESS_CODE }, headers });
  assert.equal(reply.status, 200);
  return reply.headers['set-cookie'].split(';')[0];
}

function cookieFor(data) {
  const payload = Buffer.from(JSON.stringify(data)).toString('base64url');
  const signature = crypto.createHmac('sha256', ENV.CHESS_SESSION_SECRET).update(payload).digest('base64url');
  return '__Host-chess-session=' + payload + '.' + signature;
}

function mockReply(status, body) {
  global.fetch = async (...args) => {
    calls.push(args);
    return { status, json: async () => body };
  };
}

test('unauthenticated status cannot contact the Pi', async () => {
  const reply = await request('status', { method: 'GET' });
  assert.equal(reply.status, 401);
  assert.equal(reply.body.loginRequired, true);
  assert.equal(calls.length, 0);
  assert.equal(reply.headers['cache-control'], 'no-store');
});

test('wrong code never issues a cookie or contacts the Pi', async () => {
  const reply = await request('login', { body: { code: 'incorrect-code' } });
  assert.equal(reply.status, 401);
  assert.equal(reply.headers['set-cookie'], undefined);
  assert.equal(calls.length, 0);
});

test('login creates a signed secure HTTP-only cookie without returning secrets', async () => {
  const reply = await request('login', { body: { code: ENV.CHESS_ACCESS_CODE } });
  assert.equal(reply.status, 200);
  assert.deepEqual(reply.body, { ok: true });
  const cookie = reply.headers['set-cookie'];
  assert.match(cookie, /^__Host-chess-session=/);
  for (const attribute of ['Path=/', 'HttpOnly', 'Secure', 'SameSite=Strict', 'Max-Age=86400']) {
    assert.ok(cookie.includes(attribute), `missing ${attribute}`);
  }
  const [payload, signature] = cookie.split(';')[0].split('=')[1].split('.');
  assert.equal(signature, crypto.createHmac('sha256', ENV.CHESS_SESSION_SECRET).update(payload).digest('base64url'));
  assert.match(JSON.parse(Buffer.from(payload, 'base64url')).id, /^[a-f0-9]{32}$/);
  assert.equal(reply.raw.includes(ENV.CHESS_RELAY_TOKEN), false);
  assert.equal(calls.length, 0);
});

test('authenticated status forwards only the server relay credential and verified player ID', async () => {
  const cookie = await login();
  mockReply(200, { ok: true, revision: 3, device: 'chessboard' });
  const reply = await request('status', { method: 'GET', headers: { cookie, authorization: 'Bearer browser-injected', 'x-player-session': 'browser-injected' } });
  assert.equal(reply.status, 200);
  assert.equal(reply.body.revision, 3);
  assert.equal(calls.length, 1);
  const [url, options] = calls[0];
  assert.equal(String(url), ENV.PI_API_URL + '/api/status');
  assert.equal(options.headers.Authorization, 'Bearer ' + ENV.CHESS_RELAY_TOKEN);
  assert.match(options.headers['X-Player-Session'], /^[a-f0-9]{32}$/);
  assert.equal(options.body, undefined);
  assert.equal(options.redirect, 'error');
  assert.equal(options.headers.cookie, undefined);
  assert.equal(reply.raw.includes(ENV.CHESS_RELAY_TOKEN), false);
});

test('mutations preserve request identity and revision for Pi-side deduplication', async () => {
  const cookie = await login();
  const body = { requestId: 'test-uuid', expectedRevision: 4, expectedGameId: 'test-game', from: 'e2', to: 'e4' };
  mockReply(200, { ok: true, revision: 5, uciMoves: ['e2e4', 'e7e5'] });
  const reply = await request('move', { body, headers: { cookie } });
  assert.equal(reply.status, 200);
  assert.deepEqual(JSON.parse(calls[0][1].body), body);
  assert.equal(calls[0][1].method, 'POST');
});

for (const action of ['heartbeat', 'release']) {
  test(`${action} requires a valid player session before contacting the Pi`, async () => {
    const cookies = [undefined, '__Host-chess-session=invalid', cookieFor({ id: 'a'.repeat(32), exp: Math.floor(Date.now() / 1000) - 1 })];
    for (const cookie of cookies) {
      const reply = await request(action, { body: { expectedGameId: 'current-game' }, headers: { cookie } });
      assert.equal(reply.status, 401);
      assert.equal(reply.body.loginRequired, true);
    }
    assert.equal(calls.length, 0);
  });

  test(`${action} forwards game identity and the authenticated session without browser credentials`, async () => {
    const cookie = await login();
    const player = JSON.parse(Buffer.from(cookie.split('=')[1].split('.')[0], 'base64url')).id;
    const body = { expectedGameId: 'current-game' };
    const snapshot = { ok: true, gameId: 'current-game', revision: 8, canControl: action === 'heartbeat', leaseExpiresAt: action === 'heartbeat' ? 1234567890 : 0 };
    mockReply(200, snapshot);
    const reply = await request(action, { body, headers: { cookie, authorization: 'Bearer browser-injected', 'x-player-session': 'browser-injected' } });
    assert.equal(reply.status, 200);
    assert.deepEqual(reply.body, snapshot);
    assert.equal(calls.length, 1);
    const [url, options] = calls[0];
    assert.equal(String(url), ENV.PI_API_URL + '/api/' + action);
    assert.equal(options.method, 'POST');
    assert.deepEqual(JSON.parse(options.body), body);
    assert.equal(options.headers.Authorization, 'Bearer ' + ENV.CHESS_RELAY_TOKEN);
    assert.equal(options.headers['X-Player-Session'], player);
    assert.equal(options.headers.cookie, undefined);
    assert.equal(reply.headers['set-cookie'], undefined);
    assert.equal(reply.raw.includes(ENV.CHESS_RELAY_TOKEN), false);
  });

  test(`${action} cannot bypass method, origin, or JSON checks`, async () => {
    const cookie = await login();
    const body = { expectedGameId: 'current-game' };
    assert.equal((await request(action, { method: 'GET', body, headers: { cookie } })).status, 405);
    assert.equal((await request(action, { body, headers: { cookie, origin: 'https://attacker.example.invalid' } })).status, 403);
    assert.equal((await request(action, { body, headers: { cookie, 'content-type': 'text/plain' } })).status, 415);
    assert.equal(calls.length, 0);
  });

  test(`${action} preserves Pi ownership conflicts and the current snapshot`, async () => {
    const cookie = await login();
    const conflict = { ok: false, code: 'board_busy', error: 'Another player is controlling the board.', state: { gameId: 'current-game', revision: 8, canControl: false } };
    mockReply(409, conflict);
    const reply = await request(action, { body: { expectedGameId: 'current-game' }, headers: { cookie } });
    assert.equal(reply.status, 409);
    assert.deepEqual(reply.body, conflict);
    assert.equal(calls.length, 1);
  });
}

test('re-login retains player identity while renewing the cookie', async () => {
  const first = await login();
  const second = await login({ cookie: first });
  const decode = cookie => JSON.parse(Buffer.from(cookie.split('=')[1].split('.')[0], 'base64url'));
  assert.equal(decode(first).id, decode(second).id);
});

test('tampered, expired, and malformed session cookies are rejected', async () => {
  const valid = await login();
  const cookieValues = [
    valid.slice(0, -1) + (valid.endsWith('A') ? 'B' : 'A'),
    cookieFor({ id: 'a'.repeat(32), exp: Math.floor(Date.now() / 1000) - 1 }),
    cookieFor({ id: 'not-valid', exp: Math.floor(Date.now() / 1000) + 60 }),
    '__Host-chess-session=broken',
  ];
  for (const cookie of cookieValues) {
    const reply = await request('status', { method: 'GET', headers: { cookie } });
    assert.equal(reply.status, 401);
  }
  assert.equal(calls.length, 0);
});

test('cross-site and malformed origins cannot submit commands or login', async () => {
  for (const origin of ['https://attacker.example.invalid', 'null', 'this is not a URL']) {
    const reply = await request('login', { body: { code: ENV.CHESS_ACCESS_CODE }, headers: { origin } });
    assert.equal(reply.status, 403);
  }
  assert.equal(calls.length, 0);
});

test('malformed and oversized JSON bodies fail before contacting the Pi', async () => {
  for (const body of ['{invalid', 'null', '42', '[]', [], { data: 'x'.repeat(5000) }]) {
    const reply = await request('move', { body });
    assert.equal(reply.status, 400);
  }
  const reply = await request('move', { headers: { 'content-type': 'text/plain' } });
  assert.equal(reply.status, 415);
  assert.equal(calls.length, 0);
});

test('unsupported routes and methods cannot be relayed', async () => {
  assert.equal((await request('arbitrary')).status, 404);
  assert.equal((await request('move', { method: 'GET' })).status, 405);
  assert.equal((await request('status', { method: 'POST' })).status, 405);
  assert.equal(calls.length, 0);
});

test('upstream revision conflicts keep their authoritative state and status', async () => {
  const cookie = await login();
  const body = { ok: false, error: 'Stale revision', revision: 9, gameId: 'current', uciMoves: ['e2e4', 'e7e5'] };
  mockReply(409, body);
  const reply = await request('move', { headers: { cookie } });
  assert.equal(reply.status, 409);
  assert.deepEqual(reply.body, body);
});

test('fetch failures return a safe offline message without secrets or internal URL', async () => {
  const cookie = await login();
  global.fetch = async () => { throw new Error(ENV.CHESS_RELAY_TOKEN + ' ' + ENV.PI_API_URL); };
  const reply = await request('status', { method: 'GET', headers: { cookie } });
  assert.equal(reply.status, 503);
  assert.equal(reply.body.ok, false);
  assert.match(reply.body.error, /offline/);
  for (const value of [ENV.CHESS_RELAY_TOKEN, ENV.PI_API_URL, ENV.CHESS_SESSION_SECRET]) assert.equal(reply.raw.includes(value), false);
});

test('invalid JSON upstream replies produce a safe offline response', async () => {
  const cookie = await login();
  global.fetch = async () => ({ status: 502, json: async () => { throw new SyntaxError('Private upstream detail'); } });
  const reply = await request('status', { method: 'GET', headers: { cookie } });
  assert.equal(reply.status, 503);
  assert.equal(reply.raw.includes('Private upstream detail'), false);
});

test('unsafe Pi origins never receive relay credentials', async () => {
  const cookie = await login();
  for (const origin of ['http://pi.example.invalid', 'https://user:password@pi.example.invalid']) {
    process.env.PI_API_URL = origin;
    const reply = await request('status', { method: 'GET', headers: { cookie } });
    assert.equal(reply.status, 503);
  }
  assert.equal(calls.length, 0);
});

test('logout expires the browser cookie without touching Pi state', async () => {
  const cookie = await login();
  const reply = await request('logout', { headers: { cookie } });
  assert.equal(reply.status, 200);
  assert.match(reply.headers['set-cookie'], /Max-Age=0/);
  assert.match(reply.headers['set-cookie'], /HttpOnly; Secure; SameSite=Strict/);
  assert.equal(calls.length, 0);
});

test('missing server configuration fails closed', async () => {
  delete process.env.CHESS_RELAY_TOKEN;
  const reply = await request('login', { body: { code: ENV.CHESS_ACCESS_CODE } });
  assert.equal(reply.status, 503);
  assert.equal(reply.headers['set-cookie'], undefined);
  assert.equal(calls.length, 0);
});
