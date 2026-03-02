/**
 * NOC Sentinel — WhatsApp Gateway
 * Gives each AI employee their own WhatsApp number.
 * Uses Baileys (unofficial WA multi-device library).
 *
 * Sessions are stored in ./sessions/<empId>/ — once connected,
 * the employee never needs to re-scan unless they log out.
 *
 * On first connection each employee sends an intro message to the admin
 * number so they can be added to the team group.
 *
 * Communicates with the FastAPI backend on NOC_API (default: http://localhost:8000)
 * Exposes REST API on WA_PORT (default: 3001) for the frontend to query status & QR codes.
 */

const {
  default: makeWASocket,
  DisconnectReason,
  useMultiFileAuthState,
  fetchLatestBaileysVersion,
} = require('@whiskeysockets/baileys');

const express  = require('express');
const axios    = require('axios');
const QRCode   = require('qrcode');
const fs       = require('fs');
const path     = require('path');
const pino     = require('pino');

// ── Configuration ─────────────────────────────────────────────────────────────
const NOC_API         = process.env.NOC_API         || 'http://localhost:8000';
const WA_PORT         = parseInt(process.env.WA_PORT || '3001');
const INTERNAL_SECRET = process.env.WA_SECRET        || 'noc-wa-internal-2025';
const INTRO_NUMBER    = process.env.INTRO_NUMBER     || '9647884078078'; // Admin's WhatsApp number

const EMPLOYEES = {
  aria:   { name: 'ARIA',   color: '#00d4ff', role: 'NOC Analyst' },
  nexus:  { name: 'NEXUS',  color: '#a855f7', role: 'Infrastructure Engineer' },
  cipher: { name: 'CIPHER', color: '#ff8c00', role: 'Security Analyst' },
  vega:   { name: 'VEGA',   color: '#4ade80', role: 'Site Reliability Engineer' },
};

// ── Intro-sent tracking (persisted to disk so restarts don't re-send) ─────────
const SESSIONS_DIR = path.join(__dirname, 'sessions');
const INTRO_FILE   = path.join(SESSIONS_DIR, 'intro_sent.json');

let introSent = {};
try {
  introSent = JSON.parse(fs.readFileSync(INTRO_FILE, 'utf8'));
} catch {
  introSent = {};
}

function saveIntroSent() {
  try {
    fs.mkdirSync(SESSIONS_DIR, { recursive: true });
    fs.writeFileSync(INTRO_FILE, JSON.stringify(introSent, null, 2));
  } catch (e) {
    log('system', 'Could not save intro_sent.json:', e.message);
  }
}

// ── State ──────────────────────────────────────────────────────────────────────
const sessions   = {};  // empId -> WASocket
const qrCodes    = {};  // empId -> base64 data URL
const status     = {};  // empId -> 'disconnected'|'connecting'|'qr'|'connected'
const phoneNums  = {};  // empId -> phone number string
const msgLog     = {};  // empId -> last 30 messages [{from, text, reply, ts}]
// Conversation store: empId -> { jid -> [{from, text, ts, isMe, name}] }
const conversations = {};

function log(empId, ...args) {
  console.log(`[${new Date().toISOString()}] [${empId.toUpperCase()}]`, ...args);
}

// ── AI Call (via NOC FastAPI sync endpoint) ────────────────────────────────────
async function callAI(empId, userMessage, fromNumber) {
  const r = await axios.post(`${NOC_API}/api/office/run-sync`, {
    employee:      empId,
    task_type:     'custom',
    custom_task:   userMessage,
    provider:      'claude',
    model_id:      '',
    history:       [],
    whatsapp_from: fromNumber,
  }, {
    timeout: 90000,
    headers: { 'X-WA-Secret': INTERNAL_SECRET },
  });
  return r.data?.response || 'I had trouble processing that. Please try again.';
}

// ── Per-employee message log ───────────────────────────────────────────────────
function addToLog(empId, from, text, reply) {
  if (!msgLog[empId]) msgLog[empId] = [];
  msgLog[empId].unshift({ from, text, reply, ts: Date.now() });
  if (msgLog[empId].length > 30) msgLog[empId].pop();
}

function addToConversation(empId, jid, entry) {
  if (!conversations[empId])      conversations[empId] = {};
  if (!conversations[empId][jid]) conversations[empId][jid] = [];
  conversations[empId][jid].push(entry);
  // Keep last 100 messages per conversation
  if (conversations[empId][jid].length > 100) conversations[empId][jid].shift();
}

function getContactName(msg) {
  return msg.pushName || msg.key?.remoteJid?.split('@')[0] || 'Unknown';
}

// ── Send intro message to admin number ────────────────────────────────────────
async function sendIntroMessage(empId, sock) {
  if (introSent[empId]) {
    log(empId, 'Intro already sent — skipping.');
    return;
  }
  const emp = EMPLOYEES[empId];
  const introMsg =
    `👋 Hi! I'm *${emp.name}*, your AI ${emp.role} at NOC Sentinel.\n\n` +
    `I'm now online and ready to help with network monitoring, incident analysis, ` +
    `and real-time operational tasks.\n\n` +
    `Please add me to the NOC team group so I can collaborate with the rest of the AI team! 🤖\n\n` +
    `_— ${emp.name} | NOC Sentinel AI_`;

  try {
    // Wait a few seconds for WA to fully initialise before sending
    await new Promise(r => setTimeout(r, 4000));
    const jid = `${INTRO_NUMBER}@s.whatsapp.net`;
    await sock.sendMessage(jid, { text: introMsg });
    introSent[empId] = { ts: Date.now(), phone: phoneNums[empId] };
    saveIntroSent();
    log(empId, `Sent intro message to +${INTRO_NUMBER}`);
  } catch (e) {
    log(empId, 'Failed to send intro message:', e.message);
  }
}

// ── Connect one employee ───────────────────────────────────────────────────────
async function connectEmployee(empId) {
  const sessionDir = path.join(SESSIONS_DIR, empId);
  fs.mkdirSync(sessionDir, { recursive: true });

  status[empId] = 'connecting';
  log(empId, 'Initializing session...');

  const { state, saveCreds } = await useMultiFileAuthState(sessionDir);
  const { version }          = await fetchLatestBaileysVersion();

  const sock = makeWASocket({
    version,
    auth:                state,
    printQRInTerminal:   false,
    logger:              pino({ level: 'silent' }),
    browser:             ['NOC Sentinel — ' + EMPLOYEES[empId].name, 'Chrome', '3.0'],
    syncFullHistory:     false,
    markOnlineOnConnect: true,
  });

  sessions[empId] = sock;

  // Persist credentials whenever they update
  sock.ev.on('creds.update', saveCreds);

  // Connection lifecycle
  sock.ev.on('connection.update', async (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      try {
        qrCodes[empId] = await QRCode.toDataURL(qr);
        status[empId]  = 'qr';
        log(empId, 'QR code ready — scan with WhatsApp');
      } catch (e) {
        log(empId, 'QR generation error:', e.message);
      }
    }

    if (connection === 'close') {
      const code      = lastDisconnect?.error?.output?.statusCode;
      const loggedOut = code === DisconnectReason.loggedOut;
      status[empId]    = 'disconnected';
      qrCodes[empId]   = null;
      phoneNums[empId] = null;
      log(empId, `Disconnected (code ${code}). Logged out: ${loggedOut}`);
      if (!loggedOut) {
        log(empId, 'Reconnecting in 8s...');
        setTimeout(() => connectEmployee(empId), 8000);
      }
    } else if (connection === 'open') {
      status[empId]    = 'connected';
      qrCodes[empId]   = null;
      const id         = sock.user?.id || '';
      phoneNums[empId] = id.split(':')[0].split('@')[0];
      log(empId, `Connected! Phone: +${phoneNums[empId]}`);

      // Send intro message to admin if not already sent
      sendIntroMessage(empId, sock).catch(e =>
        log(empId, 'Intro message error:', e.message)
      );
    }
  });

  // Incoming messages
  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    if (type !== 'notify') return;

    for (const msg of messages) {
      if (msg.key.remoteJid === 'status@broadcast') continue;

      const from    = msg.key.remoteJid;
      const isGroup = from.endsWith('@g.us');
      const isMe    = !!msg.key.fromMe;

      const text = (
        msg.message?.conversation ||
        msg.message?.extendedTextMessage?.text ||
        msg.message?.imageMessage?.caption ||
        msg.message?.videoMessage?.caption ||
        ''
      ).trim();

      // Track all messages in conversations (both sent and received)
      if (text) {
        addToConversation(empId, from, {
          from:   isMe ? (phoneNums[empId] || 'me') : from.replace('@s.whatsapp.net','').replace('@g.us',''),
          name:   isMe ? EMPLOYEES[empId].name : getContactName(msg),
          text,
          ts:     msg.messageTimestamp ? Number(msg.messageTimestamp) * 1000 : Date.now(),
          isMe,
          isGroup,
        });
      }

      // Only auto-reply to incoming individual DMs
      if (isMe || isGroup || !text) continue;

      log(empId, `DM from ${from}: "${text.substring(0, 80)}"`);

      try {
        await sock.readMessages([msg.key]);
        await sock.sendPresenceUpdate('composing', from);

        const reply = await callAI(empId, text, from.replace('@s.whatsapp.net', ''));

        await sock.sendPresenceUpdate('paused', from);

        // Split long replies (WA limit ~4096 chars)
        if (reply.length > 3800) {
          const chunks = reply.match(/.{1,3800}(\s|$)/gs) || [reply];
          for (const chunk of chunks) {
            await sock.sendMessage(from, { text: chunk.trim() });
            await new Promise(r => setTimeout(r, 500));
          }
        } else {
          await sock.sendMessage(from, { text: reply });
        }

        // Track the AI reply in conversations too
        addToConversation(empId, from, {
          from: phoneNums[empId] || 'me',
          name: EMPLOYEES[empId].name,
          text: reply,
          ts:   Date.now(),
          isMe: true, isGroup: false,
        });

        addToLog(empId, from.replace('@s.whatsapp.net', ''), text, reply.substring(0, 200));
        log(empId, `Replied to ${from}`);

      } catch (e) {
        log(empId, 'Error processing message:', e.message);
        try {
          await sock.sendMessage(from, {
            text: `Sorry, I ran into an issue. Please try again in a moment. — ${EMPLOYEES[empId].name}`,
          });
        } catch {}
      }
    }
  });
}

// ── Start all employees ────────────────────────────────────────────────────────
async function startAll() {
  log('system', 'Starting WhatsApp gateway for all employees...');
  for (const empId of Object.keys(EMPLOYEES)) {
    try {
      await connectEmployee(empId);
    } catch (e) {
      log(empId, 'Startup error:', e.message);
      status[empId] = 'disconnected';
    }
    // Small delay between connections to avoid rate limiting
    await new Promise(r => setTimeout(r, 2000));
  }
}

// ── REST API ───────────────────────────────────────────────────────────────────
const app = express();
app.use(express.json());

// CORS for FastAPI frontend
app.use((req, res, next) => {
  res.header('Access-Control-Allow-Origin', '*');
  res.header('Access-Control-Allow-Headers', 'Content-Type, X-WA-Secret');
  res.header('Access-Control-Allow-Methods', 'GET, POST, DELETE');
  if (req.method === 'OPTIONS') return res.sendStatus(200);
  next();
});

// GET /status — all employee statuses + QR codes
app.get('/status', (req, res) => {
  const result = {};
  for (const empId of Object.keys(EMPLOYEES)) {
    result[empId] = {
      status:      status[empId]    || 'disconnected',
      phone:       phoneNums[empId] || null,
      qr:          qrCodes[empId]   || null,
      name:        EMPLOYEES[empId].name,
      color:       EMPLOYEES[empId].color,
      role:        EMPLOYEES[empId].role,
      msgs:        (msgLog[empId] || []).length,
      intro_sent:  !!introSent[empId],
    };
  }
  res.json(result);
});

// GET /log/:empId — recent message log
app.get('/log/:empId', (req, res) => {
  const { empId } = req.params;
  res.json(msgLog[empId] || []);
});

// GET /groups/:empId — list WhatsApp groups this employee belongs to
app.get('/groups/:empId', async (req, res) => {
  const { empId } = req.params;
  if (!sessions[empId] || status[empId] !== 'connected') {
    return res.status(400).json({ error: `${empId} not connected` });
  }
  try {
    const groupData = await sessions[empId].groupFetchAllParticipating();
    const groups = Object.values(groupData).map(g => ({
      jid:          g.id,
      name:         g.subject || g.id,
      participants: (g.participants || []).length,
      owner:        g.owner || null,
    }));
    groups.sort((a, b) => a.name.localeCompare(b.name));
    res.json(groups);
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// POST /send/:empId — send a message from this employee (to individual or group JID)
app.post('/send/:empId', async (req, res) => {
  const { empId } = req.params;
  const { to, message } = req.body;
  if (!sessions[empId] || status[empId] !== 'connected') {
    return res.status(400).json({ error: `${empId} not connected` });
  }
  if (!to || !message) {
    return res.status(400).json({ error: 'to and message are required' });
  }
  try {
    // Accept phone numbers, @s.whatsapp.net JIDs, and @g.us group JIDs
    const jid = to.includes('@') ? to : `${to}@s.whatsapp.net`;
    await sessions[empId].sendMessage(jid, { text: message });
    res.json({ ok: true, jid });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// DELETE /logout/:empId — log out and delete session
app.delete('/logout/:empId', async (req, res) => {
  const { empId } = req.params;
  try {
    if (sessions[empId] && status[empId] === 'connected') {
      await sessions[empId].logout();
    }
  } catch {}
  const sessionDir = path.join(SESSIONS_DIR, empId);
  try { fs.rmSync(sessionDir, { recursive: true, force: true }); } catch {}
  // Also clear intro_sent so a fresh scan sends intro again
  delete introSent[empId];
  saveIntroSent();
  status[empId]    = 'disconnected';
  qrCodes[empId]   = null;
  phoneNums[empId] = null;
  delete sessions[empId];
  res.json({ ok: true });
});

// POST /reconnect/:empId — force reconnect
app.post('/reconnect/:empId', async (req, res) => {
  const { empId } = req.params;
  try {
    if (sessions[empId]) {
      try { await sessions[empId].end(); } catch {}
    }
    await connectEmployee(empId);
    res.json({ ok: true });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// GET /conversations/:empId — list all tracked conversations (DMs + groups) with last message
app.get('/conversations/:empId', (req, res) => {
  const { empId } = req.params;
  const convs = conversations[empId] || {};
  const result = Object.entries(convs).map(([jid, msgs]) => {
    const last = msgs[msgs.length - 1] || {};
    return {
      jid,
      name:     last.name || jid,
      isGroup:  jid.endsWith('@g.us'),
      lastMsg:  last.text ? last.text.substring(0, 100) : '',
      lastTs:   last.ts || 0,
      count:    msgs.length,
      unread:   msgs.filter(m => !m.isMe).length,
    };
  }).sort((a, b) => b.lastTs - a.lastTs);
  res.json(result);
});

// GET /conversations/:empId/:jid — get messages for a specific conversation
app.get('/conversations/:empId/:jid', (req, res) => {
  const { empId } = req.params;
  const jid = decodeURIComponent(req.params.jid);
  const msgs = (conversations[empId] || {})[jid] || [];
  res.json(msgs.slice(-50)); // last 50 messages
});

// POST /reply/:empId — manually send a message (DM or group) and log it
app.post('/reply/:empId', async (req, res) => {
  const { empId } = req.params;
  const { to, message } = req.body;
  if (!sessions[empId] || status[empId] !== 'connected') {
    return res.status(400).json({ error: `${empId} not connected` });
  }
  if (!to || !message) {
    return res.status(400).json({ error: 'to and message are required' });
  }
  try {
    const jid = to.includes('@') ? to : `${to}@s.whatsapp.net`;
    await sessions[empId].sendMessage(jid, { text: message });
    // Track in conversation
    addToConversation(empId, jid, {
      from:    phoneNums[empId] || 'me',
      name:    EMPLOYEES[empId].name,
      text:    message,
      ts:      Date.now(),
      isMe:    true,
      isGroup: jid.endsWith('@g.us'),
    });
    res.json({ ok: true, jid });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// POST /call-link/:empId — generate a WhatsApp click-to-call/chat link
app.post('/call-link/:empId', (req, res) => {
  const { phone } = req.body;
  if (!phone) return res.status(400).json({ error: 'phone required' });
  const cleaned = phone.replace(/\D/g, '');
  res.json({
    ok: true,
    wa_link:   `https://wa.me/${cleaned}`,
    call_link: `https://wa.me/${cleaned}?text=Calling...`,
  });
});

// Health check
app.get('/health', (req, res) => res.json({ ok: true, ts: Date.now() }));

app.listen(WA_PORT, '0.0.0.0', () => {
  log('system', `WhatsApp gateway API listening on port ${WA_PORT}`);
});

// ── Boot ───────────────────────────────────────────────────────────────────────
startAll().catch(e => {
  console.error('Fatal startup error:', e);
  process.exit(1);
});
