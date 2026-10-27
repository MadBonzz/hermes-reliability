"""Match LM Studio requests to evaluation turns without guessing among repeated runs.

Only matched benchmark request bodies are exported. Unrelated conversation contents
are not copied into the project. Legacy requests can have multiple report candidates.
"""

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.runtime_support import utc_now, write_json_atomic

REQUEST = re.compile(
    r"\[(?P<timestamp>[^\]\r\n]+)\]\[DEBUG\] Received request: POST to (?P<endpoint>\S+) with body\s*"
)
MARKER = re.compile(r"\[EVAL run=([^\s\]]+) scenario=([^\s\]]+) turn=(\d+)\]")


def normalize(text):
    return " ".join(text.replace("\\", "/").split())


def prompt_matches(logged, expected):
    logged, expected = normalize(logged), normalize(expected)
    if logged == expected:
        return True
    pieces = re.split(r"\.\.\.\s*<Truncated in logs>\s*\.\.\.", logged)
    if len(pieces) < 2 or sum(map(len, pieces)) < 30:
        return False
    return bool(re.fullmatch(".*".join(re.escape(p) for p in pieces), expected, flags=re.S))


def read_requests(text):
    decoder = json.JSONDecoder()
    for match in REQUEST.finditer(text):
        entry = {
            "timestamp_local": match["timestamp"],
            "endpoint": match["endpoint"],
            "line": text.count("\n", 0, match.start()) + 1,
        }
        try:
            body, _ = decoder.raw_decode(text, match.end())
            entry["body"] = body
        except (ValueError, TypeError) as exc:
            entry["parse_error"] = str(exc)
        yield entry


def report_index(paths):
    entries = []
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        for scenario in data.get("reports", []):
            for turn in scenario.get("turns", []):
                entries.append(
                    {
                        "report": str(path.resolve()),
                        "model": data.get("model"),
                        "run_id": data.get("run_id"),
                        "scenario_id": scenario["id"],
                        "turn_number": turn["turn_number"],
                        "prompt": normalize(turn.get("sent_prompt", turn["prompt"])),
                    }
                )
    return entries


def match_request(body, index):
    messages = body.get("messages", [])
    users = [m.get("content") for m in messages if isinstance(m, dict) and m.get("role") == "user"]
    if not users or not isinstance(users[-1], str):
        return {"status": "unmatched", "candidates": []}
    prompt = users[-1]
    kind = "conversation"
    if prompt.startswith("Review the conversation above and update the skill"):
        return {"status": "unmatched", "candidates": [], "request_kind": "background_skill_review"}
    if prompt.startswith(
        (
            "You've reached the maximum number of tool-calling",
            "You just executed tool calls but returned an empty",
        )
    ):
        kind = "forced_final_or_empty_response_recovery"
        users = users[:-1]
        if not users or not isinstance(users[-1], str):
            return {"status": "unmatched", "candidates": [], "request_kind": kind}
        prompt = users[-1]
    marker = MARKER.search(prompt)
    if marker:
        return {
            "status": "explicit_marker",
            "run_id": marker[1],
            "scenario_id": marker[2],
            "turn_number": int(marker[3]),
            "candidates": [],
            "request_kind": kind,
        }
    candidates = [
        {k: v for k, v in entry.items() if k != "prompt"}
        for entry in index
        if entry["model"] == body.get("model") and prompt_matches(prompt, entry["prompt"])
    ]
    return {
        "status": "unique_report_match"
        if len(candidates) == 1
        else "ambiguous"
        if candidates
        else "unmatched",
        "candidates": candidates,
        "request_kind": kind,
    }


def _infer_segments(matches):
    """Group sequential turns, annotating matched requests without resolving ambiguity."""
    segments = []
    previous = None
    for item in matches:
        candidates = item["candidates"]
        ids = {(c["scenario_id"], c["turn_number"]) for c in candidates}
        if item["status"] == "explicit_marker":
            ids = {(item["scenario_id"], item["turn_number"])}
        if len(ids) != 1:
            previous = None
            continue
        sid, turn = next(iter(ids))
        restart = (
            previous is None
            or previous["model"] != item["model"]
            or sid < previous["scenario_id"]
            or (sid == previous["scenario_id"] and turn < previous["turn_number"])
            or (
                sid == previous["scenario_id"]
                and turn == 1
                and item["message_count"] <= 2 < previous["message_count"]
            )
        )
        if restart:
            segments.append(
                {
                    "segment": len(segments) + 1,
                    "model": item["model"],
                    "first_timestamp_local": item["timestamp_local"],
                    "turns": [],
                    "requests": 0,
                }
            )
        segment = segments[-1]
        segment["last_timestamp_local"] = item["timestamp_local"]
        segment["requests"] += 1
        if [sid, turn] not in segment["turns"]:
            segment["turns"].append([sid, turn])
        item["inferred_segment"] = segment["segment"]
        previous = dict(
            model=item["model"],
            scenario_id=sid,
            turn_number=turn,
            message_count=item["message_count"],
        )
    return segments


def correlate(log_root, reports, output):
    index = report_index(reports)
    files, matches = [], []
    for path in sorted(Path(log_root).rglob("*.log")):
        raw = path.read_bytes()
        text = raw.decode("utf-8", errors="replace")
        timestamps = re.findall(r"^\[(\d{4}-\d\d-\d\d [^\]]+)\]", text, re.M)
        stats = Counter()
        models = Counter()
        for request in read_requests(text):
            stats["post_requests"] += 1
            if "parse_error" in request:
                stats["unparseable_bodies"] += 1
                continue
            body = request.pop("body")
            if not isinstance(body, dict):
                stats["non_object_bodies"] += 1
                continue
            if body.get("model"):
                models[body["model"]] += 1
            matched = match_request(body, index)
            stats[matched["status"]] += 1
            if matched.get("request_kind") == "background_skill_review":
                stats["background_skill_review"] += 1
            if matched["status"] != "unmatched":
                evidence = (
                    Path(output).parent
                    / "matched_requests"
                    / f"request_{len(matches) + 1:05d}.json"
                )
                write_json_atomic(evidence, body)
                matches.append(
                    {
                        "log": str(path),
                        **request,
                        **matched,
                        "model": body.get("model"),
                        "message_count": len(body.get("messages", [])),
                        "truncated_prompt": "<Truncated in logs>" in str(body.get("messages", [])),
                        "tool_names": [
                            t.get("function", {}).get("name") for t in body.get("tools", [])
                        ],
                        "request_evidence": str(evidence),
                    }
                )
        files.append(
            {
                "path": str(path),
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "first_timestamp_local": timestamps[0] if timestamps else None,
                "last_timestamp_local": timestamps[-1] if timestamps else None,
                "models_in_requests": dict(models),
                "counts": dict(stats),
            }
        )
    segments = _infer_segments(matches)
    result = {
        "inspected_at_utc": utc_now(),
        "log_root": str(log_root),
        "report_turns_indexed": len(index),
        "files": files,
        "matched_requests": matches,
        "inferred_segments": segments,
        "limitations": [
            "Server timestamps have no timezone in this format.",
            "No timestamp-only attribution: identical legacy prompts across runs remain ambiguous.",
            "Segments are inferred from scenario/turn resets and model changes; they are not server session IDs.",
            "A unique report match identifies compatible content, not proof that no other run used that prompt.",
            "Missing/empty logs cannot reconstruct requests or responses.",
            "Request bodies may contain prior tool results; response pairing is not inferred from proximity.",
        ],
    }
    write_json_atomic(output, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-root", required=True)
    parser.add_argument("--reports", nargs="+", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Destination JSON; matched request evidence is saved beside it",
    )
    args = parser.parse_args()
    if not Path(args.log_root).is_dir():
        parser.error("Log directory does not exist")
    result = correlate(args.log_root, args.reports, args.output)
    print(
        f"Inspected {len(result['files'])} log files; matched {len(result['matched_requests'])} requests. Evidence: {args.output}"
    )


if __name__ == "__main__":
    main()
