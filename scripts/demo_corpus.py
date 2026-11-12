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
# One habit per project, so the Waste tab shows each cause clear its 10% floor
# (waste.MIN_SHARE): web re-reads files, api leaves the cache to expire, mobile
# spawns subagents, docs keeps big shell output in context.
HABITS = {"acme/web": "repeated_reads", "demo/api": "idle_gap",
          "acme/mobile": "subagent_preamble", "demo/docs": "resident_output"}
TITLES = {"acme/web": ["Fix checkout form validation", "Add dark mode toggle", "Refactor auth middleware"],
          "demo/api": ["Paginate the orders endpoint", "Investigate slow search query", "Add rate limiting"],
          "acme/mobile": ["Port settings screen to new nav", "Offline sync for drafts", "Crash on image upload"],
          "demo/docs": ["Rewrite getting-started guide", "Fix broken links in API reference", "Changelog for 2.4"]}
MODELS = ["claude-sonnet-4-6"] * 6 + ["claude-opus-4-8"] * 2 + ["claude-haiku-4-5"]


def main(out):
    rnd = random.Random(7)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    out = Path(out)
    for s in range(60):
        proj = PROJECTS[s % len(PROJECTS)]
        habit = HABITS[proj]
        cwd = "/work/" + proj
        d = out / cwd.replace("/", "-")
        d.mkdir(parents=True, exist_ok=True)
        sid = f"demo-{s:04d}-0000-4000-8000-000000000000"
        model = rnd.choice(MODELS)
        clock = now - timedelta(days=rnd.random() * 13, hours=rnd.random() * 8)
        lines = []
        ctx = 8000
        n = rnd.randint(12, 60)
        gaps = {n // 3, 2 * n // 3} if habit == "idle_gap" else set()
        heavy = False  # the previous turn's habit makes this turn write a lot of cache

        def turn(t, content, creation, read, sidechain=None):
            rec = {"type": "assistant", "sessionId": sid, "cwd": cwd, "gitBranch": "main",
                   "timestamp": clock.strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "message": {"id": f"msg-{s}-{t}", "model": model, "stop_reason": "end_turn",
                               "usage": {"input_tokens": rnd.randint(50, 900),
                                         "output_tokens": rnd.randint(100, 1800),
                                         "cache_read_input_tokens": read,
                                         "cache_creation_input_tokens": creation},
                               "content": content}}
            if sidechain:
                rec.update(isSidechain=True, agentId=sidechain)
            lines.append(json.dumps(rec))

        for t in range(n):
            clock += timedelta(minutes=rnd.randint(25, 40) if t in gaps else 2)
            if t == 0:
                lines.append(json.dumps({"type": "user", "sessionId": sid, "cwd": cwd,
                                         "gitBranch": "main", "timestamp": clock.strftime("%Y-%m-%dT%H:%M:%SZ")}))
                lines.append(json.dumps({"type": "ai-title", "sessionId": sid,
                                         "aiTitle": TITLES[proj][s // 4 % 3]}))
            content = []
            if habit == "repeated_reads" and t % 3 == 0:
                content = [{"type": "tool_use", "id": f"tu-{s}-{t}", "name": "Read",
                            "input": {"file_path": f"{cwd}/src/file{t % 2}.py"}}]
            elif habit == "resident_output" and t % 4 == 0:
                content = [{"type": "tool_use", "id": f"tu-{s}-{t}", "name": "Bash",
                            "input": {"command": "make test"}}]
            if t in gaps:  # cache expired: the whole prefix is written again
                creation, read = ctx, 0
            else:
                creation = rnd.randint(6000, 14000) if heavy else rnd.randint(200, 1500)
                read = ctx
            ctx += rnd.randint(400, 3000)
            turn(t, content, creation, read)
            for call in content:  # its result: sized to the largest next write (resident_output caps there)
                lines.append(json.dumps({"type": "user", "sessionId": sid, "cwd": cwd, "timestamp": clock.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                         "message": {"role": "user", "content": [
                                             {"type": "tool_result", "tool_use_id": call["id"], "content": "x" * 49_000}]}}))
            heavy = bool(content)
            if habit == "subagent_preamble" and t % 10 == 5:
                agent = f"agent-{s}-{t}"
                for k in range(3):  # first write is the subagent's preamble
                    clock += timedelta(seconds=20)
                    turn(f"{t}-{agent}-{k}", [], rnd.randint(15000, 25000) if k == 0 else rnd.randint(200, 900),
                         0 if k == 0 else 18000, sidechain=agent)
        (d / f"{sid}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1])
