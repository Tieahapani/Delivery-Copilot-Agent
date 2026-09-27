from dotenv import load_dotenv
load_dotenv() 
import argparse
import json
import os
import sys
import time
 
# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
 
from src.graph import build_graph
from evals.golden_dataset import GOLDEN_CASES 

def run_single_case(graph, case: dict) -> dict: 
    """Run one test case through the graph and compare to expected."""
 
    event = case["event"]
    expected = case["expected"]

    initial_state = {
        "event": event, 
        "triage": None, 
        "route_decision": None, 
        "reroute": None, 
        "escalation" : None, 
        "notification": None, 
        "resolution": None, 
    }

    start = time.time()
    try:
        result = graph.invoke(initial_state)
        elapsed_ms = round((time.time() - start) * 1000)
        error = None
    except Exception as e:
        elapsed_ms = round((time.time() - start) * 1000)
        return {
            "id": case["id"],
            "category": case["category"],
            "description": case["description"],
            "error": str(e),
            "elapsed_ms": elapsed_ms,
            "decision_match": False,
            "urgency_match": False,
            "channel_match": False,
            "json_fallback": True,
        }
    
    triage = result.get("triage", {})
    notification = result.get("notification", {})
 
    actual_urgency = triage.get("urgency", "UNKNOWN")
    actual_decision = triage.get("decision", "unknown")
    actual_channel = notification.get("channel", "unknown")
    reasoning = triage.get("reasoning", "")
 
    # Check if JSON fallback was triggered
    json_fallback = "Failed to parse" in reasoning
 
    return {
        "id": case["id"],
        "category": case["category"],
        "description": case["description"],
        "error": error,
        "elapsed_ms": elapsed_ms,
        # Actual vs expected
        "expected_urgency": expected["urgency"],
        "actual_urgency": actual_urgency,
        "urgency_match": expected["urgency"] == actual_urgency,
        "expected_decision": expected["decision"],
        "actual_decision": actual_decision,
        "decision_match": expected["decision"] == actual_decision,
        "expected_channel": expected.get("channel"),
        "actual_channel": actual_channel,
        "channel_match": expected.get("channel") == actual_channel,
        "json_fallback": json_fallback,
        "reasoning": reasoning,
    }
 
 
def print_result_line(r: dict, verbose: bool):
    """Print one result line."""
 
    # Status icon
    if r.get("error"):
        icon = "💥"
    elif r["decision_match"] and r["urgency_match"]:
        icon = "✅"
    elif r["decision_match"]:
        icon = "🟡"
    else:
        icon = "❌"
 
    line = (
        f"  {icon} {r['id']} | "
        f"D:{r.get('actual_decision', 'err'):12s} (exp:{r.get('expected_decision', '?'):12s}) | "
        f"U:{r.get('actual_urgency', 'err'):8s} (exp:{r.get('expected_urgency', '?'):8s}) | "
        f"{r['elapsed_ms']:5d}ms"
    )
    print(line)
 
    if verbose and not r.get("error"):
        print(f"       Reasoning: {r.get('reasoning', 'n/a')}")
        if not r["channel_match"]:
            print(f"       Channel: {r.get('actual_channel')} (expected {r.get('expected_channel')})")
        print()
 
    if r.get("error"):
        print(f"       ERROR: {r['error']}")
        print()
 
 
def print_scorecard(results: list):
    """Print the final evaluation scorecard."""
 
    total = len(results)
    errors = sum(1 for r in results if r.get("error"))
    successful = [r for r in results if not r.get("error")]
 
    if not successful:
        print("  No successful runs to score.")
        return
 
    decision_correct = sum(1 for r in successful if r["decision_match"])
    urgency_correct = sum(1 for r in successful if r["urgency_match"])
    channel_correct = sum(1 for r in successful if r["channel_match"])
    both_correct = sum(1 for r in successful if r["decision_match"] and r["urgency_match"])
    json_failures = sum(1 for r in successful if r["json_fallback"])
    latencies = [r["elapsed_ms"] for r in successful]
 
    print("=" * 65)
    print("  SCORECARD")
    print("=" * 65)
    print(f"  Total cases:         {total}")
    print(f"  Successful runs:     {len(successful)}")
    print(f"  Errors/crashes:      {errors}")
    print()
    print(f"  Decision accuracy:   {decision_correct}/{len(successful)} ({decision_correct/len(successful)*100:.1f}%)")
    print(f"  Urgency accuracy:    {urgency_correct}/{len(successful)} ({urgency_correct/len(successful)*100:.1f}%)")
    print(f"  Channel accuracy:    {channel_correct}/{len(successful)} ({channel_correct/len(successful)*100:.1f}%)")
    print(f"  Full match (D+U):    {both_correct}/{len(successful)} ({both_correct/len(successful)*100:.1f}%)")
    print()
    print(f"  JSON parse failures: {json_failures}/{len(successful)}")
    print()
    print(f"  Latency (avg):       {sum(latencies)//len(latencies)} ms")
    print(f"  Latency (min):       {min(latencies)} ms")
    print(f"  Latency (max):       {max(latencies)} ms")
    print(f"  Total eval time:     {sum(latencies)/1000:.1f} s")
    print()
 
    # Per-category breakdown
    categories = {}
    for r in successful:
        cat = r["category"]
        if cat not in categories:
            categories[cat] = {"total": 0, "decision_ok": 0, "urgency_ok": 0}
        categories[cat]["total"] += 1
        if r["decision_match"]:
            categories[cat]["decision_ok"] += 1
        if r["urgency_match"]:
            categories[cat]["urgency_ok"] += 1
 
    print("  Per-category breakdown:")
    for cat, stats in categories.items():
        d_pct = stats["decision_ok"] / stats["total"] * 100
        u_pct = stats["urgency_ok"] / stats["total"] * 100
        print(f"    {cat:25s}  D:{d_pct:5.1f}%  U:{u_pct:5.1f}%  (n={stats['total']})")
 
    print("=" * 65)
 
    # List misses
    misses = [r for r in successful if not r["decision_match"] or not r["urgency_match"]]
    if misses:
        print()
        print("  MISSES (cases that didn't match expected):")
        print()
        for r in misses:
            flags = []
            if not r["decision_match"]:
                flags.append(f"decision: got {r['actual_decision']}, expected {r['expected_decision']}")
            if not r["urgency_match"]:
                flags.append(f"urgency: got {r['actual_urgency']}, expected {r['expected_urgency']}")
            print(f"    {r['id']}: {r['description']}")
            for f in flags:
                print(f"      {f}")
            print(f"      reasoning: {r['reasoning']}")
            print()
 
 
def main():
    parser = argparse.ArgumentParser(description="Run golden eval on copilot graph")
    parser.add_argument("--verbose", action="store_true", help="Print reasoning for each case")
    parser.add_argument("--category", type=str, default=None,
                        help="Run only cases in this category (e.g., edge_case)")
    parser.add_argument("--case", type=str, default=None,
                        help="Run a single case by ID (e.g., GOLD-017)")
    args = parser.parse_args()
 
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: Set ANTHROPIC_API_KEY")
        sys.exit(1)
 
    # Filter cases
    cases = GOLDEN_CASES
    if args.category:
        cases = [c for c in cases if c["category"] == args.category]
        print(f"  Filtered to category: {args.category} ({len(cases)} cases)")
    if args.case:
        cases = [c for c in cases if c["id"] == args.case]
        print(f"  Running single case: {args.case}")
 
    if not cases:
        print("  No matching cases found.")
        sys.exit(1)
 
    print("=" * 65)
    print("  Delivery Copilot Evaluation")
    print(f"  {len(cases)} test cases")
    print("=" * 65)
    print()
 
    # Build graph once
    print("  Building graph...")
    graph = build_graph()
    print("  Graph compiled.\n")
 
    # Run all cases
    results = []
    for i, case in enumerate(cases):
        print(f"  [{i+1}/{len(cases)}] {case['id']}: {case['description']}")
        result = run_single_case(graph, case)
        results.append(result)
        print_result_line(result, args.verbose)
 
    print()
 
    # Scorecard
    print_scorecard(results)
 
    # Save results
    output_path = os.path.join(os.path.dirname(__file__), "eval_results.json")
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\n  Results saved to {output_path}")
 
 
if __name__ == "__main__":
    main()
    








