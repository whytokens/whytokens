"""Provenance vocabulary: the single source for how much to trust a displayed number.
Served at GET /api/provenance; the frontend never hardcodes these words or tooltips."""

LABELS = {
    # numbers
    "official": "Token counts as reported by the provider. Totals may include scanner-estimated rows (advisor / "
                "subagent), flagged individually in Trace.",
    "local_estimate": "Computed locally: every $ figure (from our price table, not an invoice) and any "
                      "estimated token count. Treat as an estimate.",
    "unknown": "Not measurable for this source. Shown as a dash, never as zero.",
    # waste attribution
    "observed": "The cause was seen directly in the transcript (e.g. a cache-write after a tool call).",
    "inferred": "The cause is deduced from indirect signals, not seen directly.",
}


def attribution(tier):
    """Waste finding tier -> 'observed' | 'inferred'. Only the 'inferred' tier is inferred."""
    return "inferred" if tier == "inferred" else "observed"
