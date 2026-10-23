"""Write a synthetic Claude Code transcript tree for README screenshots.

    python scripts/demo_corpus.py OUT_DIR

Neutral project names, fixed seed, dates relative to today. Nothing real.
"""

import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECTS = ["acme/web", "demo/api", "acme/mobile", "demo/docs"]
MODELS = ["claude-sonnet-4-6"] * 6 + ["claude-opus-4-8"] * 2 + ["claude-haiku-4-5"]


def main(out):
    rnd = random.Random(7)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    out = Path(out)
    for s in range(60):
        proj = PROJECTS[s % len(PROJECTS)]
        cwd = "/work/" + proj
        d = out / cwd.replace("/", "-")
        d.mkdir(parents=True, exist_ok=True)
        sid = f"demo-{s:04d}-0000-4000-8000-000000000000"
        model = rnd.choice(MODELS)
        t0 = now - timedelta(days=rnd.random() * 13, hours=rnd.random() * 8)
        lines = []
        ctx = 8000
        for t in range(rnd.randint(12, 60)):
            ts = (t0 + timedelta(minutes=2 * t)).strftime("%Y-%m-%dT%H:%M:%SZ")
            if t == 0:
                lines.append(json.dumps({"type": "user", "sessionId": sid, "cwd": cwd,
                                         "gitBranch": "main", "timestamp": ts}))
            content = []
            if t % 3 == 0:  # repeated reads of the same few files feed the Waste tab
                content = [{"type": "tool_use", "id": f"tu-{s}-{t}", "name": "Read",
                            "input": {"file_path": f"{cwd}/src/file{t % 2}.py"}}]
            ctx += rnd.randint(400, 3000)
            lines.append(json.dumps({
                "type": "assistant", "sessionId": sid, "cwd": cwd, "gitBranch": "main", "timestamp": ts,
                "message": {"id": f"msg-{s}-{t}", "model": model, "stop_reason": "end_turn",
                            "usage": {"input_tokens": rnd.randint(50, 900),
                                      "output_tokens": rnd.randint(100, 1800),
                                      "cache_read_input_tokens": ctx,
                                      "cache_creation_input_tokens": rnd.randint(200, 4000)},
                            "content": content}}))
        (d / f"{sid}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1])
