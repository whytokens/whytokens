// @ts-check
// Pure helpers for the trace bands and the analytics interactions.
// No DOM, no app state: node-testable (tests/test_analytics.py).

/** Monday (UTC) of the week holding a YYYY-MM-DD date. */
export function weekOf(d) {
  const t = new Date(d + 'T00:00:00Z');
  t.setUTCDate(t.getUTCDate() - (t.getUTCDay() + 6) % 7);
  return t.toISOString().slice(0, 10);
}

/**
 * One band per user request: prompt_seq -> call count and cost. Only turns with a numeric
 * prompt_seq count; null (subagent turns, pruned transcripts) never makes a band, so a
 * session with no prompt_seq returns []. Seq 0 (work before the first prompt) is not a request.
 */
export function promptBands(turns, costOf) {
  const bands = new Map();
  for (const t of turns) {
    if (typeof t.prompt_seq !== 'number' || t.prompt_seq < 1) continue;
    const b = bands.get(t.prompt_seq) || { seq: t.prompt_seq, calls: 0, cost: 0 };
    b.calls++;
    b.cost += costOf(t);
    bands.set(t.prompt_seq, b);
  }
  return [...bands.values()].sort((a, b) => a.seq - b.seq);
}

/** Turns of one request, each with its index in the full list (tool panels address turns by it). */
export function turnsOfBand(turns, seq) {
  return turns.map((t, i) => ({ t, i })).filter(x => seq == null || x.t.prompt_seq === seq);
}

/**
 * Waterfall steps for one session: total -> each cause's attributed $ -> unattributed.
 * Attribution is a per-cause lower bound and can overlap, so the remainder is clamped at 0
 * and `over` reports any excess instead of drawing a negative step.
 */
export function sessionWaterfall(findings, sessionId, fallbackTotal = 0) {
  const mine = findings.filter(f => f.session_id === sessionId);
  if (!mine.length && !(fallbackTotal > 0)) return null;
  const total = mine.length ? mine[0].session_usd : fallbackTotal;  // no findings: all of it is unattributed
  const byCause = new Map();
  for (const f of mine) byCause.set(f.cause, (byCause.get(f.cause) || 0) + f.usd);
  const steps = [...byCause].sort((a, b) => b[1] - a[1]).map(([cause, usd]) => ({ cause, usd }));
  const attributed = steps.reduce((s, x) => s + x.usd, 0);
  return { total, steps, attributed, unattributed: Math.max(0, total - attributed), over: Math.max(0, attributed - total) };
}

/** Cause x week attributed $ over the given sessions (findings outside them are ignored). */
export function causeWeekMatrix(findings, sessions) {
  const byId = new Map(sessions.map(s => [s.full_session_id, s]));
  const cell = new Map(), weeks = new Set(), causes = new Map();
  for (const f of findings) {
    const s = byId.get(f.session_id);
    if (!s || !s.last_date) continue;
    const w = weekOf(s.last_date);
    weeks.add(w);
    causes.set(f.cause, (causes.get(f.cause) || 0) + f.usd);
    cell.set(f.cause + '\u0000' + w, (cell.get(f.cause + '\u0000' + w) || 0) + f.usd);
  }
  const ws = [...weeks].sort();
  const rows = [...causes].sort((a, b) => b[1] - a[1]).map(([cause, total]) =>
    ({ cause, total, cells: ws.map(w => cell.get(cause + '\u0000' + w) || 0) }));
  return { weeks: ws, rows, max: Math.max(0, ...rows.flatMap(r => r.cells)) };
}
