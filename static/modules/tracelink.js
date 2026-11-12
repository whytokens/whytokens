// Trace deep link: the URL <-> state mapping for a flagged turn. Pure, so a test can
// round-trip it without a DOM. Params: turn (message_id), hl (comma-joined ids), why (banner).

/** Copy a trace link's params into `params` (a URLSearchParams). Empty fields are omitted. */
export function writeTraceLink(params, { turn = '', hl = new Set(), why = '' } = {}) {
  if (turn) params.set('turn', turn);
  if (hl.size) params.set('hl', [...hl].join(','));
  if (why) params.set('why', why);
}

/** ?search string -> { turn, hl: Set, why }. Missing fields come back empty. */
export function readTraceLink(search) {
  const q = new URLSearchParams(search);
  return {
    turn: q.get('turn') || '',
    hl: new Set((q.get('hl') || '').split(',').filter(Boolean)),
    why: q.get('why') || '',
  };
}

/** Index of the row to scroll to: the turn's row, or null when the id is not in the trace. */
export function findTurnRow(turns, turn) {
  if (!turn) return null;
  const i = turns.findIndex(t => t.message_id === turn);
  return i < 0 ? null : i;
}
