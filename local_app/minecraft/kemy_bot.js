// Bot Minecraft da Kemy (Mineflayer). O "corpo" no jogo; o cerebro e a IA no app Python,
// que manda comandos por TCP (JSON por linha) e recebe estado + eventos (chat).
//
// Config por variaveis de ambiente:
//   MC_HOST (default localhost)  MC_PORT (25565)  MC_USER (Kemy)  MC_VERSION (auto: false)
//   MC_AUTH (offline|microsoft, default offline)  KEMY_MC_PORT (porta TCP local, 8079)
//
// Protocolo: o app conecta em 127.0.0.1:KEMY_MC_PORT e troca linhas JSON.
//   ->  {"cmd":"state"} | {"cmd":"say","text"} | {"cmd":"come"} | {"cmd":"follow","name"}
//       {"cmd":"stop"} | {"cmd":"goto","x","y","z"} | {"cmd":"mine","name","count"}
//       {"cmd":"collect"} | {"cmd":"attack"} | {"cmd":"place","name"} | {"cmd":"drop","name"}
//   <-  {"ok":bool,"msg":string,"state":{...}}   e eventos  {"event":"chat","user","text"}

const net = require('net');
const mineflayer = require('mineflayer');
const { pathfinder, Movements, goals } = require('mineflayer-pathfinder');
const { plugin: collectBlock } = require('mineflayer-collectblock');

const HOST = process.env.MC_HOST || 'localhost';
const PORT = parseInt(process.env.MC_PORT || '25565', 10);
const USER = process.env.MC_USER || 'Kemy';
const AUTH = process.env.MC_AUTH || 'offline';
const VERSION = process.env.MC_VERSION || false; // false = auto-detect
const TCP_PORT = parseInt(process.env.KEMY_MC_PORT || '8079', 10);

let bot = null;
let mcData = null;
let clients = [];
let followInterval = null;

function broadcast(obj) {
  const line = JSON.stringify(obj) + '\n';
  for (const c of clients) { try { c.write(line); } catch (e) {} }
}

function state() {
  if (!bot || !bot.entity) return { connected: false };
  const e = bot.entity;
  const nearbyPlayers = Object.values(bot.players || {})
    .filter(p => p.entity && p.username !== bot.username)
    .map(p => ({ name: p.username, dist: Math.round(p.entity.position.distanceTo(e.position)) }))
    .sort((a, b) => a.dist - b.dist).slice(0, 5);
  const mobs = Object.values(bot.entities || {})
    .filter(m => m.type === 'mob' || m.type === 'hostile')
    .map(m => ({ name: m.name, dist: Math.round(m.position.distanceTo(e.position)) }))
    .sort((a, b) => a.dist - b.dist).slice(0, 5);
  const inv = (bot.inventory ? bot.inventory.items() : []).map(i => `${i.name} x${i.count}`);
  return {
    connected: true,
    pos: { x: Math.round(e.position.x), y: Math.round(e.position.y), z: Math.round(e.position.z) },
    health: bot.health, food: bot.food, time: bot.time && bot.time.timeOfDay,
    players: nearbyPlayers, mobs, inventory: inv.slice(0, 20),
  };
}

function stopAll() {
  try { if (followInterval) { clearInterval(followInterval); followInterval = null; } } catch (e) {}
  try { bot.pathfinder.setGoal(null); } catch (e) {}
  try { bot.pvp && bot.pvp.stop(); } catch (e) {}
}

async function gotoXYZ(x, y, z) {
  bot.pathfinder.setGoal(new goals.GoalNear(x, y, z, 1));
  return `indo para ${x} ${y} ${z}`;
}

function followPlayer(name) {
  stopAll();
  const target = () => (name ? bot.players[name] && bot.players[name].entity
    : (Object.values(bot.players).filter(p => p.entity && p.username !== bot.username)
        .sort((a, b) => a.entity.position.distanceTo(bot.entity.position)
                       - b.entity.position.distanceTo(bot.entity.position))[0] || {}).entity);
  followInterval = setInterval(() => {
    const t = target();
    if (t) { try { bot.pathfinder.setGoal(new goals.GoalFollow(t, 2), true); } catch (e) {} }
  }, 800);
  return name ? `seguindo ${name}` : 'seguindo o jogador mais proximo';
}

async function mineBlock(name, count) {
  count = count || 1;
  const ids = [];
  const b = mcData.blocksByName[name];
  if (b) ids.push(b.id);
  // tenta variantes (ex.: "log" -> *_log)
  if (!b) for (const k in mcData.blocksByName) if (k.includes(name)) ids.push(mcData.blocksByName[k].id);
  if (!ids.length) return `nao conheco o bloco "${name}"`;
  let mined = 0;
  for (let i = 0; i < count; i++) {
    const block = bot.findBlock({ matching: ids, maxDistance: 48 });
    if (!block) break;
    try { await bot.collectBlock.collect(block); mined++; }
    catch (e) { break; }
  }
  return `minerei ${mined}x ${name}`;
}

async function collectDrops() {
  const drops = Object.values(bot.entities).filter(e => e.name === 'item' || e.objectType === 'Item');
  let n = 0;
  for (const d of drops.slice(0, 10)) {
    try { await bot.pathfinder.goto(new goals.GoalNear(d.position.x, d.position.y, d.position.z, 0)); n++; }
    catch (e) {}
  }
  return `coletei ${n} item(ns) do chao`;
}

async function attackNearest() {
  const mob = bot.nearestEntity(e => (e.type === 'mob' || e.type === 'hostile') && e.position.distanceTo(bot.entity.position) < 16);
  if (!mob) return 'nenhum inimigo por perto';
  try { bot.pathfinder.setGoal(new goals.GoalFollow(mob, 1), true); bot.attack(mob); }
  catch (e) {}
  return `atacando ${mob.name}`;
}

async function placeBlock(name) {
  const item = bot.inventory.items().find(i => i.name === name) || bot.inventory.items()[0];
  if (!item) return 'sem blocos no inventario';
  try {
    await bot.equip(item, 'hand');
    const ref = bot.blockAtCursor(5) || bot.blockAt(bot.entity.position.offset(0, -1, 0));
    await bot.placeBlock(ref, require('vec3')(0, 1, 0));
    return `coloquei ${item.name}`;
  } catch (e) { return `nao consegui colocar (${e.message})`; }
}

async function handle(obj) {
  if (!bot || !bot.entity) return { ok: false, msg: 'ainda nao entrei no servidor' };
  const c = (obj.cmd || '').toLowerCase();
  try {
    if (c === 'state') return { ok: true, state: state() };
    if (c === 'say') { bot.chat(String(obj.text || '').slice(0, 240)); return { ok: true, msg: 'falei no chat' }; }
    if (c === 'stop') { stopAll(); return { ok: true, msg: 'parei' }; }
    if (c === 'come' || c === 'follow') return { ok: true, msg: followPlayer(obj.name) };
    if (c === 'goto') return { ok: true, msg: await gotoXYZ(obj.x | 0, obj.y | 0, obj.z | 0) };
    if (c === 'mine') return { ok: true, msg: await mineBlock(obj.name, obj.count) };
    if (c === 'collect') return { ok: true, msg: await collectDrops() };
    if (c === 'attack') return { ok: true, msg: await attackNearest() };
    if (c === 'place') return { ok: true, msg: await placeBlock(obj.name) };
    return { ok: false, msg: `comando desconhecido: ${c}` };
  } catch (e) {
    return { ok: false, msg: 'erro: ' + (e && e.message) };
  }
}

function start() {
  bot = mineflayer.createBot({ host: HOST, port: PORT, username: USER, auth: AUTH, version: VERSION || undefined });
  bot.loadPlugin(pathfinder);
  bot.loadPlugin(collectBlock);

  bot.once('spawn', () => {
    mcData = require('minecraft-data')(bot.version);
    const moves = new Movements(bot, mcData);
    moves.allowParkour = true; moves.canDig = true;
    bot.pathfinder.setMovements(moves);
    broadcast({ event: 'ready', version: bot.version, msg: 'entrei no servidor' });
    bot.chat('Oi! Sou a Kemy 💙 me peça o que quiser.');
  });

  bot.on('chat', (username, message) => {
    if (username === bot.username) return;
    broadcast({ event: 'chat', user: username, text: message });
  });
  bot.on('health', () => broadcast({ event: 'health', health: bot.health, food: bot.food }));
  bot.on('death', () => broadcast({ event: 'death', msg: 'morri 😵' }));
  bot.on('kicked', (r) => broadcast({ event: 'kicked', msg: String(r) }));
  bot.on('error', (e) => broadcast({ event: 'error', msg: String(e && e.message) }));
  bot.on('end', () => { broadcast({ event: 'end', msg: 'desconectei' }); setTimeout(start, 5000); });
}

// Servidor TCP local pro app Python conversar com o bot.
const server = net.createServer((sock) => {
  clients.push(sock);
  sock.on('error', () => {});
  sock.on('close', () => { clients = clients.filter(c => c !== sock); });
  let buf = '';
  sock.on('data', async (d) => {
    buf += d.toString('utf8');
    let nl;
    while ((nl = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, nl).trim(); buf = buf.slice(nl + 1);
      if (!line) continue;
      let obj; try { obj = JSON.parse(line); } catch (e) { continue; }
      const res = await handle(obj);
      try { sock.write(JSON.stringify(res) + '\n'); } catch (e) {}
    }
  });
  try { sock.write(JSON.stringify({ event: 'hello', state: state() }) + '\n'); } catch (e) {}
});
server.listen(TCP_PORT, '127.0.0.1', () => console.log('kemy-mc bridge on ' + TCP_PORT));

start();
