// Demo data layer: generates realistic logs in the same format as the API.
// New logs are produced lazily based on elapsed time (about 3 per second),
// so no timers are needed and nothing runs at module load.

import type { Level, LogEntry, LogQuery, LogsResponse, StatsResponse } from "./types";

const SERVICES = ["order", "payment", "inventory"];
const HOSTS = ["node-1", "node-2", "node-3"];

const MESSAGES: Record<string, Record<Level, string[]>> = {
  order: {
    DEBUG: ["Cache lookup for order {n}", "Entering handler for order {n}"],
    INFO: ["Order {n} created", "Order {n} confirmed by customer", "Order {n} handed to shipping"],
    WARNING: ["Order {n} is taking longer than usual", "Order {n} retried after timeout"],
    ERROR: ["Order {n} could not be saved: database timeout", "Order {n} rejected: invalid address"],
    CRITICAL: ["Database unreachable, order {n} lost", "Out of memory while handling order {n}"],
  },
  payment: {
    DEBUG: ["Query plan chosen for payment {n}", "Token refreshed for order {n}"],
    INFO: ["Payment authorised for order {n}", "Refund issued for order {n}"],
    WARNING: ["Payment gateway slow to respond for order {n}", "3-D Secure check pending for order {n}"],
    ERROR: ["Card payment failed for order {n}", "Payment gateway returned 502 for order {n}"],
    CRITICAL: ["Payment gateway down, order {n} cannot be charged", "Ledger write failed for order {n}"],
  },
  inventory: {
    DEBUG: ["Cache lookup for SKU {n}", "Index rebuilt for SKU {n}"],
    INFO: ["Stock reserved for order {n}", "Stock level updated for SKU {n}"],
    WARNING: ["Low stock for SKU {n}", "Reservation for order {n} close to expiry"],
    ERROR: ["Stock reservation failed for order {n}", "Inventory sync failed for SKU {n}"],
    CRITICAL: ["Inventory database corrupted at SKU {n}", "Warehouse link down, order {n} cannot ship"],
  },
};

const pick = <T,>(arr: T[]): T => arr[Math.floor(Math.random() * arr.length)] as T;

function randomLevel(): Level {
  const r = Math.random();
  if (r < 0.2) return "DEBUG";
  if (r < 0.75) return "INFO";
  if (r < 0.9) return "WARNING";
  if (r < 0.97) return "ERROR";
  return "CRITICAL";
}

interface Store {
  logs: LogEntry[]; // ascending by row_id
  nextRowId: number;
  seq: Record<string, number>;
  lastTick: number;
}

let store: Store | null = null;

function makeLog(s: Store, at: number): LogEntry {
  const service = pick(SERVICES);
  const level = randomLevel();
  const host = pick(HOSTS);
  const n = 1000 + Math.floor(Math.random() * 9000);
  const key = `${service}@${host}`;
  s.seq[key] = (s.seq[key] ?? 0) + 1;
  const delay = 5 + Math.random() * 40;
  return {
    row_id: s.nextRowId++,
    id: crypto.randomUUID(),
    timestamp: new Date(at).toISOString(),
    received_at: new Date(at + delay).toISOString(),
    delay_ms: Math.round(delay * 10) / 10,
    service,
    level,
    host,
    seq: s.seq[key],
    message: pick(MESSAGES[service]![level]).replace("{n}", String(n)),
  };
}

function getStore(): Store {
  const now = Date.now();
  if (!store) {
    store = { logs: [], nextRowId: 1, seq: {}, lastTick: now };
    // Seed about two hours of history.
    for (let t = now - 2 * 60 * 60 * 1000; t < now; t += 1500 + Math.random() * 1500) {
      store.logs.push(makeLog(store, t));
    }
  }
  // Add roughly 3 logs per elapsed second since the last call.
  const elapsed = now - store.lastTick;
  const count = Math.floor((elapsed / 1000) * 3);
  for (let i = 0; i < count; i++) {
    store.logs.push(makeLog(store, store.lastTick + ((i + 1) * elapsed) / count));
  }
  if (count > 0) store.lastTick = now;
  if (store.logs.length > 20000) store.logs = store.logs.slice(-15000);
  return store;
}

export function mockGetLogs(query: LogQuery): LogsResponse {
  const s = getStore();
  const limit = Math.min(Math.max(query.limit ?? 100, 1), 1000);
  const q = query.q?.toLowerCase();
  const out: LogEntry[] = [];
  for (let i = s.logs.length - 1; i >= 0 && out.length < limit; i--) {
    const l = s.logs[i]!;
    if (query.since_id !== undefined && l.row_id <= query.since_id) break;
    if (query.service && l.service !== query.service) continue;
    if (query.level && l.level !== query.level.toUpperCase()) continue;
    if (q && !l.message.toLowerCase().includes(q)) continue;
    if (query.from && l.timestamp < query.from) continue;
    if (query.to && l.timestamp > query.to) continue;
    out.push(l);
  }
  return { count: out.length, logs: out };
}

export function mockGetStats(): StatsResponse {
  const s = getStore();
  const by_level: Record<Level, number> = { DEBUG: 0, INFO: 0, WARNING: 0, ERROR: 0, CRITICAL: 0 };
  for (const l of s.logs) by_level[l.level]++;
  return { lost_logs: 0, total_logs: s.logs.length, by_level };
}
