"use strict";
// Stream Deck+ dial for Monitor Brightness. The tray app (brightness.py) does the DDC/CI
// work; this plugin just talks to its API on 127.0.0.1. No npm dependencies: Stream Deck's
// bundled Node 20 has fetch but no WebSocket client, so a minimal one is included below.

const net = require("node:net");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");

const API = "http://127.0.0.1:47823";
const HEADERS = { "Content-Type": "application/json", "X-Monitor-Brightness": "1" };
const DEFAULTS = { monitor: "all", step: 5 };
// Made by "py brightness.py install"; pressing the dial while the tray app is offline opens it.
const SHORTCUT = path.join(process.env.APPDATA || "", "Microsoft", "Windows", "Start Menu", "Programs", "Monitor Brightness.lnk");

const args = {};
for (let i = 2; i + 1 < process.argv.length; i += 2) args[process.argv[i].replace(/^-+/, "")] = process.argv[i + 1];

const contexts = new Map(); // context -> { action, settings }
let state; // latest { instance, version, monitors } from the tray app; null while unreachable, undefined until first try
let polling = false;
let starting = false;

// --- Stream Deck connection -------------------------------------------------------------------

const deck = connect(Number(args.port), {
  open() {
    deck.send(JSON.stringify({ event: args.registerEvent, uuid: args.pluginUUID }));
  },
  message(text) {
    let msg;
    try {
      msg = JSON.parse(text);
    } catch {
      return;
    }
    const handler = handlers[msg.event];
    if (handler) Promise.resolve(handler(msg)).catch((err) => log(`${msg.event}: ${err.stack || err}`));
  },
  close() {
    process.exit(0);
  },
});

function send(event, context, payload) {
  deck.send(JSON.stringify({ event, context, payload }));
}

function log(message) {
  deck.send(JSON.stringify({ event: "logMessage", payload: { message: `[monitorbrightness] ${message}` } }));
}

const handlers = {
  willAppear({ action, context, payload }) {
    if (payload.controller !== "Encoder") return;
    contexts.set(context, { action, settings: { ...DEFAULTS, ...payload.settings } });
    render(context);
    poll();
  },

  willDisappear({ context }) {
    contexts.delete(context);
  },

  didReceiveSettings({ context, payload }) {
    const c = contexts.get(context);
    if (!c) return;
    c.settings = { ...DEFAULTS, ...payload.settings };
    render(context);
  },

  async dialRotate({ context, payload }) {
    const c = contexts.get(context);
    if (!c) return;
    const step = payload.pressed ? 1 : Number(c.settings.step) || DEFAULTS.step; // hold + turn = fine steps
    await change({ monitor: c.settings.monitor, delta: payload.ticks * step });
  },

  async dialDown({ context }) {
    const c = contexts.get(context);
    if (!c) return;
    if (!state) return startTrayApp(context);
    // Press cycles All displays -> each monitor -> All displays, skipping monitors without DDC/CI.
    const choices = ["all", ...state.monitors.filter((m) => m.brightness !== null).map((m) => m.id)];
    c.settings = { ...c.settings, monitor: choices[(choices.indexOf(c.settings.monitor) + 1) % choices.length] };
    send("setSettings", context, c.settings); // remembered across restarts, and the settings panel follows
    render(context);
  },

  async touchTap({ context }) {
    if (!state) return startTrayApp(context);
    await refresh(); // re-read the monitors, e.g. after using their own buttons
  },

  async sendToPlugin({ action, context, payload }) {
    // The settings panel asks for the monitor list when it opens.
    if (payload && payload.request === "monitors") {
      await refresh();
      deck.send(JSON.stringify({ action, event: "sendToPropertyInspector", context,
                                 payload: { monitors: state ? state.monitors : null } }));
    }
  },
};

// --- Tray app API -----------------------------------------------------------------------------

async function api(route, body) {
  const options = body === undefined ? { headers: HEADERS } : { method: "POST", headers: HEADERS, body: JSON.stringify(body) };
  const response = await fetch(API + route, options);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

function accept(next) {
  // Responses can arrive out of order; keep the newest (a new instance means the tray app restarted).
  if (!state || next.instance !== state.instance || next.version > state.version) {
    state = next;
    for (const context of contexts.keys()) render(context);
  }
}

function offline() {
  if (state === null) return;
  state = null;
  for (const context of contexts.keys()) render(context);
}

async function change(body) {
  try {
    accept(await api("/brightness", body));
  } catch {
    offline();
    poll();
  }
}

async function refresh() {
  try {
    accept(await api("/monitors?refresh=1"));
  } catch {
    offline();
  }
}

// Long-poll the tray app while any dial is visible, so changes made in the tray flyout show up
// on the touch strip right away. The server holds each request until something changes.
async function poll() {
  if (polling) return;
  polling = true;
  try {
    while (contexts.size) {
      try {
        accept(await api(state ? `/monitors?since=${state.version}` : "/monitors?refresh=1"));
      } catch {
        offline();
        await new Promise((resolve) => setTimeout(resolve, 5000)); // tray app not running; retry
      }
    }
  } finally {
    polling = false;
  }
}

async function startTrayApp(context) {
  if (starting) return;
  if (!fs.existsSync(SHORTCUT)) {
    log(`Can't start the tray app: ${SHORTCUT} is missing. Run "py brightness.py install".`);
    return send("showAlert", context);
  }
  starting = true;
  for (const ctx of contexts.keys()) render(ctx);
  // Explorer opens the shortcut, so the tray app runs on its own instead of inside Stream Deck's
  // plugin process group (which Windows ends whenever Stream Deck restarts).
  spawn("explorer.exe", [SHORTCUT], { detached: true, stdio: "ignore" })
    .on("error", (err) => log(`Starting the tray app failed: ${err}`))
    .unref();
  for (let i = 0; i < 20 && !state; i++) {
    await new Promise((resolve) => setTimeout(resolve, 500));
    await refresh();
  }
  starting = false;
  if (!state) {
    for (const ctx of contexts.keys()) render(ctx);
    send("showAlert", context);
  }
}

// --- Touch strip --------------------------------------------------------------------------------

function targets(monitor) {
  if (!state) return [];
  return monitor === "all" ? state.monitors : state.monitors.filter((m) => m.id === monitor);
}

function level(monitor) {
  const known = targets(monitor).filter((m) => m.brightness !== null);
  return known.length ? Math.round(known.reduce((sum, m) => sum + m.brightness, 0) / known.length) : null;
}

function render(context) {
  const c = contexts.get(context);
  if (!c) return;
  const monitor = c.settings.monitor;
  let title, value, percent;
  if (state === undefined) {
    [title, value, percent] = ["Brightness", "…", 0];
  } else if (!state) {
    [title, value, percent] = starting ? ["Brightness", "Starting…", 0] : ["Press to start", "Offline", 0];
  } else if (!targets(monitor).length) {
    [title, value, percent] = ["Not connected", "–", 0];
  } else {
    percent = level(monitor);
    title = monitor === "all" ? (state.monitors.length > 1 ? "All displays" : state.monitors[0].name) : targets(monitor)[0].name;
    value = percent === null ? "–" : `${percent}%`;
  }
  send("setFeedback", context, { title, value, indicator: { value: percent ?? 0 } });
}

// --- Minimal WebSocket client (RFC 6455, text frames only) ------------------------------------

function connect(port, events) {
  const socket = net.connect(port, "127.0.0.1");
  let buffer = Buffer.alloc(0);
  let open = false;
  let fragments = [];

  socket.on("connect", () => {
    const key = crypto.randomBytes(16).toString("base64");
    socket.write(
      `GET / HTTP/1.1\r\nHost: 127.0.0.1:${port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n` +
        `Sec-WebSocket-Key: ${key}\r\nSec-WebSocket-Version: 13\r\n\r\n`
    );
  });

  socket.on("data", (chunk) => {
    buffer = Buffer.concat([buffer, chunk]);
    if (!open) {
      const end = buffer.indexOf("\r\n\r\n");
      if (end < 0) return;
      if (!/^HTTP\/1\.1 101/.test(buffer.subarray(0, end).toString())) return socket.destroy();
      buffer = buffer.subarray(end + 4);
      open = true;
      events.open();
    }
    while (buffer.length >= 2) {
      const fin = buffer[0] & 0x80;
      const opcode = buffer[0] & 0x0f;
      const masked = buffer[1] & 0x80;
      let length = buffer[1] & 0x7f;
      let offset = 2;
      if (length === 126) {
        if (buffer.length < 4) return;
        length = buffer.readUInt16BE(2);
        offset = 4;
      } else if (length === 127) {
        if (buffer.length < 10) return;
        length = Number(buffer.readBigUInt64BE(2));
        offset = 10;
      }
      const maskAt = offset;
      if (masked) offset += 4;
      if (buffer.length < offset + length) return;
      let data = Buffer.from(buffer.subarray(offset, offset + length));
      if (masked) for (let i = 0; i < data.length; i++) data[i] ^= buffer[maskAt + (i & 3)];
      buffer = buffer.subarray(offset + length);

      if (opcode === 0x8) {
        socket.end(frame(data, 0x8)); // close handshake
        return events.close();
      }
      if (opcode === 0x9) socket.write(frame(data, 0xa)); // ping -> pong
      if (opcode <= 0x2) {
        fragments.push(data);
        if (fin) {
          events.message(Buffer.concat(fragments).toString("utf8"));
          fragments = [];
        }
      }
    }
  });

  socket.on("end", events.close);
  socket.on("close", events.close);
  socket.on("error", () => {}); // "close" follows

  // Client frames must be masked.
  function frame(data, opcode) {
    const mask = crypto.randomBytes(4);
    const length = data.length;
    const header = length < 126 ? Buffer.alloc(2) : length < 65536 ? Buffer.alloc(4) : Buffer.alloc(10);
    header[0] = 0x80 | opcode;
    if (length < 126) header[1] = 0x80 | length;
    else if (length < 65536) {
      header[1] = 0x80 | 126;
      header.writeUInt16BE(length, 2);
    } else {
      header[1] = 0x80 | 127;
      header.writeBigUInt64BE(BigInt(length), 2);
    }
    const body = Buffer.alloc(length);
    for (let i = 0; i < length; i++) body[i] = data[i] ^ mask[i & 3];
    return Buffer.concat([header, mask, body]);
  }

  return {
    send(text) {
      if (open) socket.write(frame(Buffer.from(text, "utf8"), 0x1));
    },
  };
}
