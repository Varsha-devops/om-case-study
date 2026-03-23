#!/usr/bin/env python3
"""
tfplan-validator — Validate a Terraform plan JSON file before applying.

Rules:
  1. Only `create` and `update` (modify) actions are allowed.
     `delete`, `replace` (destroy-then-create), or any unknown action blocks the apply.
  2. For `update` actions, the ONLY attribute allowed to change is `tags`,
     and within `tags` the ONLY key allowed to change is `GitCommitHash`.
     Any other field change, or any tag change other than `GitCommitHash`, blocks the apply.
  3. `no-op` and `read` actions are informational and always allowed.

Usage:
  python3 script.py <tfplan.json> [<tfplan2.json> ...]
  python3 script.py *.json
"""

import json
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ALLOWED_ACTIONS   = {"create", "update", "no-op", "read"}
BLOCKED_ACTIONS   = {"delete"}          # standalone delete
REPLACE_PAIR      = {"delete", "create"} # replace = ["delete","create"]

GREEN  = "\033[32m"
RED    = "\033[31m"
YELLOW = "\033[33m"
BOLD   = "\033[1m"
RESET  = "\033[0m"


def _fmt_action(actions: list[str]) -> str:
    return f"[{', '.join(actions)}]"


def _diff_dicts(before: dict, after: dict) -> dict:
    """Return keys whose values differ between before and after."""
    all_keys = set(before) | set(after)
    return {
        k: {"before": before.get(k), "after": after.get(k)}
        for k in all_keys
        if before.get(k) != after.get(k)
    }


def validate_update(change: dict) -> list[str]:
    """
    For an `update` action, verify that only tags.GitCommitHash changed.
    Returns a list of human-readable violation strings (empty = OK).
    """
    before: dict = change.get("before") or {}
    after:  dict = change.get("after")  or {}

    # Collect all top-level attribute differences (excluding 'tags' for now)
    non_tag_changes = {
        k: v for k, v in _diff_dicts(before, after).items()
        if k != "tags"
    }

    # Terraform marks computed/unknown fields as True in after_unknown;
    # those are expected to be unknown until apply — treat them as non-violations
    # ONLY if they appear in after_unknown AND are absent from both before and after.
    after_unknown: dict = change.get("after_unknown") or {}
    filtered_non_tag = {}
    for k, diff in non_tag_changes.items():
        if after_unknown.get(k) is True and diff["before"] is None and diff["after"] is None:
            # Fully unknown computed attribute — not a real diff
            continue
        filtered_non_tag[k] = diff

    violations = []

    # Flag non-tag field changes
    for attr, diff in filtered_non_tag.items():
        violations.append(
            f"  ✗ Non-tag attribute '{attr}' changed: "
            f"{json.dumps(diff['before'])} → {json.dumps(diff['after'])}"
        )

    # Inspect tag-level changes
    before_tags: dict = before.get("tags") or {}
    after_tags:  dict = after.get("tags")  or {}
    tag_diffs    = _diff_dicts(before_tags, after_tags)

    for tag_key, diff in tag_diffs.items():
        if tag_key != "GitCommitHash":
            violations.append(
                f"  ✗ Tag '{tag_key}' changed (only 'GitCommitHash' is permitted): "
                f"{json.dumps(diff['before'])} → {json.dumps(diff['after'])}"
            )

    return violations


# ---------------------------------------------------------------------------
# Core validator
# ---------------------------------------------------------------------------

def validate_plan(plan_path: str) -> bool:
    """
    Validate a single plan file.
    Returns True if the apply may proceed, False otherwise.
    """
    path = Path(plan_path)
    print(f"\n{'='*60}")
    print(f"{BOLD}Plan file:{RESET} {path.name}")
    print("=" * 60)

    # --- Load ---
    try:
        with open(path) as fh:
            plan = json.load(fh)
    except FileNotFoundError:
        print(f"{RED}ERROR: File not found: {path}{RESET}")
        return False
    except json.JSONDecodeError as exc:
        print(f"{RED}ERROR: Invalid JSON — {exc}{RESET}")
        return False

    # --- Top-level error flag ---
    if plan.get("errored"):
        print(f"{RED}✗ BLOCKED — The plan itself errored during generation.{RESET}")
        return False

    resource_changes: list[dict] = plan.get("resource_changes", [])

    if not resource_changes:
        print(f"{YELLOW}⚠ No resource changes found — nothing to apply.{RESET}")
        # An empty plan is technically safe; treat as proceed.
        return True

    all_violations: list[str] = []
    summary_lines:  list[str] = []

    for rc in resource_changes:
        address = rc.get("address", "<unknown>")
        change  = rc.get("change", {})
        actions: list[str] = change.get("actions", [])
        action_set = set(actions)

        # ── no-op / read ── informational, always fine
        if action_set <= {"no-op", "read"}:
            summary_lines.append(f"  {GREEN}✔{RESET} {address} {_fmt_action(actions)}")
            continue

        # ── replace (delete + create) ── never allowed
        if action_set == REPLACE_PAIR or (len(actions) > 1 and "delete" in action_set):
            msg = (
                f"  ✗ {address} {_fmt_action(actions)} — "
                "Resource replacement (destroy + create) is not permitted."
            )
            all_violations.append(msg)
            summary_lines.append(f"  {RED}✗{RESET} {address} {_fmt_action(actions)}")
            continue

        # ── delete ── never allowed
        if action_set & BLOCKED_ACTIONS:
            msg = (
                f"  ✗ {address} {_fmt_action(actions)} — "
                "Destruction of resources is not permitted."
            )
            all_violations.append(msg)
            summary_lines.append(f"  {RED}✗{RESET} {address} {_fmt_action(actions)}")
            continue

        # ── create ── always fine
        if action_set == {"create"}:
            summary_lines.append(f"  {GREEN}✔{RESET} {address} {_fmt_action(actions)}")
            continue

        # ── update ── only GitCommitHash tag may change
        if action_set == {"update"}:
            update_violations = validate_update(change)
            if update_violations:
                all_violations.extend(
                    [f"  ✗ {address} [update]:"] + update_violations
                )
                summary_lines.append(f"  {RED}✗{RESET} {address} {_fmt_action(actions)}")
            else:
                summary_lines.append(f"  {GREEN}✔{RESET} {address} {_fmt_action(actions)}")
            continue

        # ── anything else (unknown action) ── block
        msg = (
            f"  ✗ {address} {_fmt_action(actions)} — "
            f"Unrecognised action(s); apply blocked."
        )
        all_violations.append(msg)
        summary_lines.append(f"  {RED}✗{RESET} {address} {_fmt_action(actions)}")

    # --- Report ---
    print("\nResource summary:")
    for line in summary_lines:
        print(line)

    if all_violations:
        print(f"\n{RED}{BOLD}✗ APPLY BLOCKED — the following issues must be resolved:{RESET}")
        for v in all_violations:
            print(f"{RED}{v}{RESET}")
        return False
    else:
        print(f"\n{GREEN}{BOLD}✔ APPLY APPROVED — all changes are within policy.{RESET}")
        return True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    files   = sys.argv[1:]
    results = {}

    for plan_file in files:
        results[plan_file] = validate_plan(plan_file)

    # Final summary when multiple files are given
    if len(files) > 1:
        print(f"\n{'='*60}")
        print(f"{BOLD}Overall results:{RESET}")
        for f, ok in results.items():
            status = f"{GREEN}APPROVED{RESET}" if ok else f"{RED}BLOCKED{RESET}"
            print(f"  {status}  {f}")

    # Exit 1 if any plan is blocked
    if not all(results.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
