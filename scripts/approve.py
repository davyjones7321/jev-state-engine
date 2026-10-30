#!/usr/bin/env python3
"""scripts/approve.py

Interactive approval CLI for test command proposals (Phase 3B).
Displays exact proposal details with secret redaction and enforces:
"approval authorizes execution, not a mechanical pass".
Upon approval, writes the trusted repository configuration entry into .jev/config.json
and records external owner trust under <JEV_HOME>/trust/<repo_id>.json.
Never executes the command.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Add src to sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from jev.proposals import (
    APPROVAL_DISCLAIMER,
    approve_proposal,
    format_proposal_screen,
    list_proposals,
    load_proposal,
    redact_data_structure,
    redact_secrets_in_text,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Review and approve a command proposal.")
    parser.add_argument("proposal_id", nargs="?", default=None, help="Proposal ID to approve (e.g. prop-1a2b3c4d5e6f)")
    parser.add_argument("--repo-dir", "-r", type=str, default=".", help="Path to repository root (defaults to current directory)")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt for non-interactive use")
    parser.add_argument("--approved-by", type=str, default="owner", help="Name or identity of the approver")
    parser.add_argument("--list", "-l", action="store_true", help="List all proposals in the repository")
    args = parser.parse_args()

    repo_dir = Path(args.repo_dir).resolve()

    if args.list or not args.proposal_id:
        proposals = list_proposals(repo_dir)
        if not proposals:
            print(f"No command proposals found in {repo_dir / '.jev' / 'proposals'}")
            sys.exit(0)
        print(f"Command proposals in {repo_dir}:")
        for p in proposals:
            print(f"  [{p.status.upper()}] {p.id} -> {' '.join(p.exact_argv)} ({p.source_evidence})")
        if not args.proposal_id:
            sys.exit(0)

    proposal = load_proposal(args.proposal_id, repo_dir)
    if proposal is None:
        print(f"Error: proposal '{args.proposal_id}' not found in {repo_dir / '.jev' / 'proposals'}", file=sys.stderr)
        sys.exit(1)

    # Verify proposal identity hasn't been tampered with
    is_valid, err = proposal.verify_identity()
    if not is_valid:
        print(f"Error: Proposal identity verification failed: {err}", file=sys.stderr)
        sys.exit(1)

    screen_output = format_proposal_screen(proposal)
    print(screen_output)

    if proposal.status == "approved":
        print(f"\nProposal '{proposal.id}' is already approved (approved_by: {proposal.approved_by} at {proposal.approved_at}).")
        sys.exit(0)

    if not args.yes:
        try:
            choice = input("\nApprove this proposal? [y/N]: ").strip().lower()
        except EOFError:
            choice = "n"
        if choice not in ("y", "yes"):
            print("Approval cancelled.")
            sys.exit(1)

    try:
        cfg, trust_rec = approve_proposal(proposal, repo_dir, approved_by=args.approved_by)
        print(f"\n[SUCCESS] Proposal '{proposal.id}' approved.")
        print(f"  Configuration saved: {repo_dir / '.jev' / 'config.json'}")
        print(f"  Trust record recorded: <JEV_HOME>/trust/{trust_rec.repo_id}.json")
    except Exception as e:
        print(f"Error during approval: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
