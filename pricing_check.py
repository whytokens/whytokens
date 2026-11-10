"""`whytokens pricing check`: review diff of the shipped price table against LiteLLM.

The tool's only outbound network call, and only when the user runs it (never during scan or
the dashboard). LiteLLM's JSON is the source; OpenRouter is a cross-check, never a source.
Output is a review diff for a human to apply by hand: nothing here writes pricing.py.
Responses are cached under DATA_DIR/pricing-cache so the check also runs offline.
"""

import json
import math
import os
import re
import urllib.request
from datetime import datetime
from pathlib import Path

import config
import pricing

LITELLM_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
OPENROUTER_URL = "https://openrouter.ai/api/v1/models"
HOSTS = ("raw.githubusercontent.com", "openrouter.ai")
FIELDS = ("input", "output", "cache_read", "cache_write", "cache_write_1h")
# LiteLLM per-token key for each shipped field (USD per token; the table is USD per million)
_LITELLM = {"input": "input_cost_per_token", "output": "output_cost_per_token",
            "cache_read": "cache_read_input_token_cost", "cache_write": "cache_creation_input_token_cost",
            "cache_write_1h": "cache_creation_input_token_cost_above_1hr"}
_OPENROUTER = {"input": "prompt", "output": "completion", "cache_read": "input_cache_read",
               "cache_write": "input_cache_write"}
TOLERANCE = 0.005  # relative; rounding noise between sources is not a change


def canonical(model_id):
    """Fold provider prefixes, dots, dates and bedrock/vertex suffixes onto the shipped key style:
    'anthropic/claude-opus-4.8', 'us.anthropic.claude-opus-4-8-v1:0', 'claude-opus-4-8-20260101',
    'vertex_ai/claude-opus-4-8@20260101' all -> 'claude-opus-4-8'."""
    m = model_id.lower().split("/")[-1]
    m = re.sub(r"^(?:[a-z]{2}\.)?(?:anthropic|openai)\.", "", m)
    m = re.sub(r"(?:-v\d+:\d+|:\d+)$", "", m)
    m = re.sub(r"(?:[@-]\d{8}|-\d{4}-\d{2}-\d{2})$", "", m)
    return re.sub(r"(?<=\d)\.(?=\d)", "-", m) if m.startswith("claude-") else m


def cache_dir():
    return config.DATA_DIR / "pricing-cache"


def _valid(name, data):
    if name == "litellm":
        return isinstance(data, dict) and any(isinstance(e, dict) and "input_cost_per_token" in e for e in data.values())
    return isinstance(data, dict) and isinstance(data.get("data"), list)


def load(name, url, offline=False, fetch=None):
    """Return (data, note). Network first unless offline; on failure (or a response that is not the
    expected shape) fall back to the cache, which a bad response never overwrites."""
    path = cache_dir() / f"{name}.json"
    err = None
    if not offline:
        try:
            if fetch:
                data = fetch(url)
            else:
                with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "whytokens"}), timeout=20) as r:
                    data = json.load(r)
            if not _valid(name, data):
                raise ValueError("unexpected response shape")
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"fetched_at": datetime.now().isoformat(timespec="seconds"), "data": data}),
                           encoding="utf-8")
            os.replace(tmp, path)
            return data, f"{name}: fetched {url}"
        except Exception as e:  # network, DNS, bad JSON, bad shape: the cache decides
            err = f"{type(e).__name__}: {e}"
    why = "offline" if offline else f"fetch failed ({err})"
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if _valid(name, cached["data"]):
            return cached["data"], f"{name}: cache from {cached['fetched_at']} ({why})"
        err = "cache has an unexpected shape"
    except FileNotFoundError:
        err = err or "no cache"
    except (ValueError, KeyError, TypeError, OSError) as e:
        err = f"unreadable cache ({type(e).__name__})"
    return None, f"{name}: unavailable ({why}; {err})"


def _rate(x):
    """Per-token USD -> per-million, or None unless a finite non-negative number."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x * 1e6 if math.isfinite(x) and x >= 0 else None


def _litellm_rates(raw):
    """{canonical: (rates per million, tiered)}. Collisions resolve deterministically, not by JSON order:
    the exact canonical key beats an alias, a direct-provider entry beats a cloud reseller, then the
    greater key (later date stamp) wins."""
    out, best = {}, {}
    for key, e in raw.items():
        if not isinstance(e, dict):
            continue
        c = canonical(key)
        rates = {f: r for f, k in _LITELLM.items() if (r := _rate(e.get(k))) is not None}
        if "input" not in rates:
            continue
        rank = (key.lower() == c, e.get("litellm_provider") in ("anthropic", "openai"), key)
        if c in best and best[c] >= rank:
            continue
        best[c] = rank
        out[c] = (rates, any("_above_" in k and "1hr" not in k for k in e), rank[1])
    return out


def _openrouter_rates(raw):
    out = {}
    for e in (raw or {}).get("data", []):
        p = e.get("pricing") if isinstance(e, dict) else None
        p = p if isinstance(p, dict) else {}
        rates = {f: r for f, k in _OPENROUTER.items() if (r := _rate(p.get(k))) is not None}
        if rates:
            out[canonical(str(e.get("id", "")))] = rates
    return out


def _differs(a, b):
    return abs(a - b) > TOLERANCE * max(abs(a), abs(b), 1e-9)


def diff(litellm, openrouter=None):
    """Review lines: shipped vs LiteLLM, annotated with the OpenRouter cross-check."""
    src, orr = _litellm_rates(litellm), _openrouter_rates(openrouter)
    lines, changed = [], 0
    for model, entry in pricing.PRICING.items():
        active = pricing._resolve_tiers(entry)  # tiered models are compared on today's tier
        note = f" (shipped has {len(entry)} dated tiers; compared the active one)" if isinstance(entry, list) else ""
        if model not in src:
            lines.append(f"NOT IN SOURCE  {model}{note}")
            continue
        rates, tiered, _ = src[model]
        diffs = [f for f in FIELDS if f in rates and f in active and _differs(rates[f], active[f])]
        check = orr.get(model)
        for f in diffs:
            cross = ("not in OpenRouter" if check is None else
                     "OpenRouter has no such field" if f not in check else
                     f"OpenRouter {check[f]:.4g} " + ("agrees" if not _differs(check[f], rates[f]) else "DISAGREES"))
            lines.append(f"CHANGED  {model}.{f}: shipped {active[f]:.4g} -> source {rates[f]:.4g} per M  [{cross}]{note}")
        changed += bool(diffs)
        if tiered:
            lines.append(f"NOTE     {model}: source lists long-context tiers (above N tokens) the base rate here ignores")
    shipped = set(pricing.PRICING)
    for model in sorted(m for m, (_, _, direct) in src.items()
                        if direct and m not in shipped and re.match(r"claude-(?:opus|sonnet|haiku|fable|mythos)-|gpt-[5-9]", m)):
        lines.append(f"NEW IN SOURCE  {model}")
    lines.append(f"{changed} shipped model(s) differ from the source; review by hand, nothing was applied.")
    return lines


def check(offline=False, fetch=None):
    """Returns (report lines, ok)."""
    litellm, n1 = load("litellm", LITELLM_URL, offline, fetch)
    orr, n2 = load("openrouter", OPENROUTER_URL, offline, fetch)
    head = [n1, n2, "source: LiteLLM; OpenRouter is a cross-check only"]
    if litellm is None:
        return head + ["No LiteLLM data: run once online, or retry later."], False
    return head + diff(litellm, orr), True
