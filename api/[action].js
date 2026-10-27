const crypto = require('node:crypto');
const COOKIE = '__Host-chess-session';
const SESSION_SECONDS = 24 * 60 * 60;
function equal(a, b) {
  return crypto.timingSafeEqual(crypto.createHash('sha256').update(String(a)).digest(), crypto.createHash('sha256').update(String(b)).digest());
}
function sign(value) { return crypto.createHmac('sha256', process.env.CHESS_SESSION_SECRET).update(value).digest('base64url'); }
function cookie(value, seconds) { return `${COOKIE}=${value}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=${seconds}`; }
function session(req) {
  const entry = (req.headers.cookie || '').split(';').map(x=>x.trim()).find(x=>x.startsWith(COOKIE+'='));
  if (!entry) return null;
  const token = entry.slice(COOKIE.length+1), [payload, signature] = token.split('.');
  if (!payload || !signature || !equal(sign(payload), signature)) return null;
  try { const data=JSON.parse(Buffer.from(payload,'base64url')); return /^[a-f0-9]{32}$/.test(data.id) && data.exp > Date.now()/1000 ? data.id : null; } catch { return null; }
}
function json(res, status, data) { res.statusCode=status;res.setHeader('Content-Type','application/json');res.end(JSON.stringify(data)); }
module.exports = async function handler(req,res) {
  res.setHeader('Cache-Control','no-store'); res.setHeader('X-Content-Type-Options','nosniff');
  const action = new URL(req.url,'https://placeholder.invalid').pathname.split('/').filter(Boolean).pop();
  const permitted = {login:'POST',logout:'POST',status:'GET',new:'POST',move:'POST',undo:'POST',heartbeat:'POST',release:'POST'};
  if (!permitted[action]) return json(res,404,{ok:false,error:'Unknown route.'});
  if (req.method !== permitted[action]) return json(res,405,{ok:false,error:'Method not allowed.'});
  if (!process.env.CHESS_SESSION_SECRET || !process.env.CHESS_ACCESS_CODE || !process.env.PI_API_URL || !process.env.CHESS_RELAY_TOKEN) return json(res,503,{ok:false,error:'The Pi connection is not configured yet.'});
  if (req.method==='POST') {
    if (String(req.headers['content-type']||'').split(';')[0].trim().toLowerCase()!=='application/json') return json(res,415,{ok:false,error:'JSON required.'});
    const origin=req.headers.origin;
    if(origin) { try { if(new URL(origin).origin !== 'https://'+req.headers.host) return json(res,403,{ok:false,error:'Request origin is not allowed.'}); } catch {return json(res,403,{ok:false,error:'Invalid origin.'});} }
  }
  let body=req.body || {};
  if(typeof body==='string') {try{body=JSON.parse(body)}catch{return json(res,400,{ok:false,error:'Invalid JSON.'});}}
  if(!body || typeof body!=='object' || Array.isArray(body) || JSON.stringify(body).length>4096) return json(res,400,{ok:false,error:'Invalid request.'});
  if(action==='login') {
    if(typeof body.code!=='string' || !equal(body.code,process.env.CHESS_ACCESS_CODE)) {
      await new Promise(r=>setTimeout(r,500)); return json(res,401,{ok:false,error:'That access code is not correct.'});
    }
    const payload=Buffer.from(JSON.stringify({id:session(req)||crypto.randomBytes(16).toString('hex'),exp:Math.floor(Date.now()/1000)+SESSION_SECONDS})).toString('base64url');
    res.setHeader('Set-Cookie',cookie(payload+'.'+sign(payload),SESSION_SECONDS));return json(res,200,{ok:true});
  }
  if(action==='logout'){res.setHeader('Set-Cookie',cookie('',0));return json(res,200,{ok:true});}
  const player=session(req); if(!player) return json(res,401,{ok:false,error:'Enter your board access code to connect.',loginRequired:true});
  try {
    const origin=new URL(process.env.PI_API_URL);
    if(origin.protocol!=='https:' || origin.username || origin.password) throw Error('Invalid Pi origin');
    const reply=await fetch(new URL('/api/'+action,origin),{method:req.method,headers:{Authorization:'Bearer '+process.env.CHESS_RELAY_TOKEN,'Content-Type':'application/json','X-Player-Session':player},body:req.method==='POST'?JSON.stringify(body):undefined,signal:AbortSignal.timeout(25000),redirect:'error'});
    const data=await reply.json();
    if(data===null || typeof data!=='object') throw Error('Invalid Pi response');
    return json(res,reply.status,data);
  } catch {return json(res,503,{ok:false,error:'The Raspberry Pi is offline or did not reply. Your game stays saved; reconnect before moving again.'});}
};
