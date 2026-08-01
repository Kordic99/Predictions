#!/usr/bin/env python3
"""Refresh current Chance Liga player identities and season totals from Livesport."""

from __future__ import annotations

import argparse
import copy
import json
import os
import tempfile
from pathlib import Path

from update_rosters import (
    LIVESPORT_TEAM_CONFIG,
    TEAM_ORDER,
    attach_livesport,
    compute_changes,
    normalize,
    now_iso,
    scrape_livesport,
    validate,
)


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        delete=False,
        dir=path.parent,
        prefix=path.name + ".",
        suffix=".tmp",
    ) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def roster_projection(players: list[dict]) -> list[dict]:
    """Compare real roster/stat changes without creating timestamp-only commits."""

    output = []
    for player in players:
        row = copy.deepcopy(player)
        row.pop("livesportRosterVerifiedAt", None)
        stats = row.get("livesportSeasonStats")
        if isinstance(stats, dict):
            stats.pop("checkedAt", None)
        output.append(row)
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("rosters-live.json"))
    parser.add_argument("--audit-output", type=Path, default=Path("roster-audit-live.json"))
    args = parser.parse_args()
    if not args.output.exists():
        raise RuntimeError(f"{args.output}: live roster file does not exist")

    payload = json.loads(args.output.read_text(encoding="utf-8"))
    baseline = payload.get("players") or []
    if not isinstance(baseline, list) or not baseline:
        raise RuntimeError(f"{args.output}: missing players array")

    checked_at = now_iso()
    livesport_clubs = {
        team: scrape_livesport(team, slug, team_id)
        for team, (slug, team_id) in LIVESPORT_TEAM_CONFIG.items()
    }
    players, reconciliation = attach_livesport(
        copy.deepcopy(baseline), livesport_clubs, baseline, checked_at
    )
    validation = validate(players)

    if roster_projection(players) == roster_projection(baseline):
        print("Livesport rosters and season totals have not changed.")
        return

    baseline_values = {
        str(player.get("transfermarktPlayerId")): player
        for player in baseline
        if player.get("transfermarktPlayerId")
    }
    changes = compute_changes(baseline, baseline_values, players)
    position_order = {"GK": 0, "D": 1, "M": 2, "A": 3}
    team_index = {team: index for index, team in enumerate(TEAM_ORDER)}
    players.sort(
        key=lambda player: (
            team_index[player["team"]],
            position_order[player["pos"]],
            normalize(player["name"]),
        )
    )
    rosters = {
        team: {
            position: [
                player["name"]
                for player in players
                if player["team"] == team and player["pos"] == position
            ]
            for position in ("GK", "D", "M", "A")
        }
        for team in TEAM_ORDER
    }
    payload.update(
        {
            "generatedAt": checked_at,
            "rosters": rosters,
            "players": players,
            "changes": changes,
            "validation": validation,
        }
    )
    payload.setdefault("sources", {})["livesportClubs"] = {
        team: club["sourceUrl"] for team, club in livesport_clubs.items()
    }

    audit = (
        json.loads(args.audit_output.read_text(encoding="utf-8"))
        if args.audit_output.exists()
        else {}
    )
    audit.update(
        {
            "generatedAt": checked_at,
            "changes": changes,
            "validation": validation,
            "livesportReconciliation": reconciliation,
        }
    )
    source_counts = audit.setdefault("sourceCounts", {})
    source_counts["livesportRows"] = sum(
        len(club["players"]) for club in livesport_clubs.values()
    )
    source_counts["seedRows"] = len(baseline)

    atomic_json(args.output, payload)
    atomic_json(args.audit_output, audit)
    print(
        json.dumps(
            {
                "players": validation["playerCount"],
                "livesportRows": source_counts["livesportRows"],
                "activePlayers": validation["activeLivesportPlayers"],
                "activeScorers": validation["activeLivesportScorers"],
                "added": len(changes["added"]),
                "moved": len(changes["moved"]),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
