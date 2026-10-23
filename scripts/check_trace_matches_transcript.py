"""Gate for the Trace view: for a real session, /api/session turns must equal an
independent re-parse of the transcript (last usage per message.id), turn by turn.
Usage: python scripts/check_trace_matches_transcript.py [session_id]  (default: newest finished main-only session)"""
import glob, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import api, config

sid = sys.argv[1] if len(sys.argv) > 1 else None
files = sorted(glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")), key=os.path.getmtime, reverse=True)
for f in files[3:200]:  # skip the newest (possibly live) sessions
    cand = os.path.basename(f)[:-6]
    if sid and cand != sid:
        continue
    last = {}
    for line in open(f, encoding="utf-8", errors="replace"):
        try: r = json.loads(line)
        except ValueError: continue
        m = r.get("message") or {}
        if r.get("type") == "assistant" and m.get("id") and m.get("usage") and not r.get("agentId"):
            u = m["usage"]
            last[m["id"]] = (r["timestamp"], u.get("input_tokens", 0), u.get("output_tokens", 0),
                             u.get("cache_read_input_tokens", 0), u.get("cache_creation_input_tokens", 0))
    if len(last) < 20:
        continue
    d = api.get_session_detail(cand)
    if "error" in d:
        continue
    main = [t for t in d["turns"] if t["actor"] == "main" and not t["estimated"]]
    exp = sorted(last.values())
    got = [(t["ts"], t["input"], t["output"], t["cache_read"], t["cache_creation"]) for t in main]
    bad = [i for i, (a, b) in enumerate(zip(sorted(got), exp)) if a != b]
    print(cand, "transcript turns", len(exp), "api main turns", len(got), "mismatches", len(bad))
    if len(exp) != len(got) or bad:
        for i in bad[:5]: print(" ", sorted(got)[i], exp[i])
        sys.exit(1)
    for t in d["turns"]:
        assert t["context"] == t["input"] + t["cache_read"] + t["cache_creation"]
    sys.exit(0)
print("no suitable session found"); sys.exit(2)
