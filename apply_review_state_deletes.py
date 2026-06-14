#!/usr/bin/env python3
"""Delete CWIC review-state rejects and repoint their GFX textures.

Dry-run is the default. Pass --apply to edit .gfx files and delete image files.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_MOD_ROOT = Path("Cold War Iron Curtain")
DEFAULT_UNKNOWN_GOAL = "gfx/interface/goals/goal_unknown.dds"
DEFAULT_GOAL_MARKER = "gfx/interface/goals/"
TEXT_SUFFIXES = {".txt", ".gui", ".yml", ".yaml", ".csv", ".json"}


@dataclass
class DeleteItem:
    game_path: str
    file_path: Path
    state_path: str | None
    file_name: str | None
    sprite_names: list[str] = field(default_factory=list)
    state_usage_files: list[str] = field(default_factory=list)
    decided_at: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "review_state",
        type=Path,
        help="*_review_state.json or *_delete.json from the review app",
    )
    parser.add_argument(
        "--repo-root", type=Path, help="Repository root. Auto-detected by default."
    )
    parser.add_argument(
        "--mod-root",
        type=Path,
        default=DEFAULT_MOD_ROOT,
        help="Mod root relative to repo root.",
    )
    parser.add_argument(
        "--replacement",
        default=DEFAULT_UNKNOWN_GOAL,
        help="Texture path to use in .gfx files.",
    )
    parser.add_argument(
        "--audit-out",
        type=Path,
        help="Audit JSON path. Defaults beside the input file.",
    )
    parser.add_argument(
        "--allow-non-goal-paths",
        action="store_true",
        help="Allow deleting assets outside gfx/interface/goals/.",
    )
    parser.add_argument(
        "--skip-sprite-usage-scan",
        action="store_true",
        help="Do not scan text files for sprite name usage.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually edit .gfx files and delete files.",
    )
    return parser.parse_args()


def resolve(path: Path, root: Path) -> Path:
    return path if path.is_absolute() else root / path


def find_repo_root(mod_root: Path) -> Path:
    if mod_root.is_absolute() and mod_root.is_dir():
        return mod_root.parent.resolve()

    starts = [Path.cwd(), Path(__file__).resolve().parent]
    for start in starts:
        for candidate in [start, *start.parents]:
            if (candidate / mod_root).is_dir():
                return candidate.resolve()

    return Path.cwd().resolve()


def normalize_slashes(value: str) -> str:
    return value.replace("\\", "/").strip().lstrip("/")


def normalize_game_path(value: str) -> str:
    return normalize_slashes(value).lower()


def derive_game_path(icon: dict[str, Any], mod_root: Path) -> str | None:
    game_path = icon.get("game_path")
    if isinstance(game_path, str) and game_path.strip():
        return normalize_slashes(game_path)

    raw_path = icon.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None

    normalized = normalize_slashes(raw_path)
    lower = normalized.lower()
    for marker in (DEFAULT_GOAL_MARKER, "gfx/leaders/", "gfx/interface/technologies/"):
        idx = lower.find(marker)
        if idx >= 0:
            return normalized[idx:]

    mod_name = mod_root.name.replace("\\", "/")
    marker = f"{mod_name}/"
    idx = lower.find(marker.lower())
    if idx >= 0:
        return normalized[idx + len(marker) :]

    return None


def audit_path_for(input_path: Path) -> Path:
    return input_path.with_name(f"{input_path.stem}_delete_audit.json")


def load_deleted_items(
    state_path: Path, repo_root: Path, mod_root: Path
) -> tuple[list[DeleteItem], list[dict[str, Any]]]:
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    raw_entries: list[dict[str, Any]] = []

    if isinstance(payload, dict) and isinstance(payload.get("decisions"), dict):
        for state_key, entry in payload["decisions"].items():
            if isinstance(entry, dict) and entry.get("decision") == "delete":
                icon = entry.get("icon") if isinstance(entry.get("icon"), dict) else {}
                raw_entries.append(
                    {"state_key": state_key, "entry": entry, "icon": icon}
                )
    elif isinstance(payload, dict) and isinstance(payload.get("items"), list):
        for entry in payload["items"]:
            if isinstance(entry, dict) and entry.get("decision") in (None, "delete"):
                raw_entries.append(
                    {"state_key": entry.get("path"), "entry": entry, "icon": entry}
                )
    else:
        raise ValueError(
            "Input must be a review_state.json with decisions or a delete export with items."
        )

    items: list[DeleteItem] = []
    skipped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_entries:
        entry = raw["entry"]
        icon = raw["icon"]
        game_path = derive_game_path(icon, mod_root)
        if not game_path:
            skipped.append(
                {
                    "state_key": raw.get("state_key"),
                    "reason": "could_not_derive_game_path",
                }
            )
            continue

        key = normalize_game_path(game_path)
        if key in seen:
            skipped.append(
                {"game_path": game_path, "reason": "duplicate_delete_decision"}
            )
            continue
        seen.add(key)

        gfx = icon.get("gfx") if isinstance(icon.get("gfx"), dict) else {}
        sprite_names = [
            name for name in gfx.get("sprite_names", []) if isinstance(name, str)
        ]
        usage_files = [
            name for name in gfx.get("usage_files", []) if isinstance(name, str)
        ]
        items.append(
            DeleteItem(
                game_path=game_path,
                file_path=mod_root / game_path,
                state_path=icon.get("path")
                if isinstance(icon.get("path"), str)
                else raw.get("state_key"),
                file_name=icon.get("file_name")
                if isinstance(icon.get("file_name"), str)
                else Path(game_path).name,
                sprite_names=sprite_names,
                state_usage_files=usage_files,
                decided_at=entry.get("decidedAt")
                if isinstance(entry.get("decidedAt"), str)
                else None,
            )
        )

    return items, skipped


def gfx_files(mod_root: Path) -> list[Path]:
    return sorted(path for path in mod_root.rglob("*.gfx") if path.is_file())


def text_files(mod_root: Path) -> list[Path]:
    return sorted(
        path
        for path in mod_root.rglob("*")
        if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES
    )


def count_case_insensitive(haystack: bytes, needle: str) -> int:
    if not needle:
        return 0
    return len(
        re.findall(re.escape(needle.encode("utf-8")), haystack, flags=re.IGNORECASE)
    )


def replace_case_insensitive(
    haystack: bytes, needle: str, replacement: str
) -> tuple[bytes, int]:
    pattern = re.compile(re.escape(needle.encode("utf-8")), flags=re.IGNORECASE)
    return pattern.subn(replacement.encode("utf-8"), haystack)


def find_gfx_references(
    items: list[DeleteItem], mod_root: Path
) -> dict[str, list[dict[str, Any]]]:
    references: dict[str, list[dict[str, Any]]] = {item.game_path: [] for item in items}
    for gfx_path in gfx_files(mod_root):
        data = gfx_path.read_bytes()
        rel_path = gfx_path.relative_to(mod_root).as_posix()
        for item in items:
            count = count_case_insensitive(data, item.game_path)
            if count:
                references[item.game_path].append({"file": rel_path, "count": count})
    return references


def scan_sprite_usages(items: list[DeleteItem], mod_root: Path) -> dict[str, list[str]]:
    names_by_item = {
        item.game_path: [name.encode("utf-8") for name in item.sprite_names]
        for item in items
    }
    usage: dict[str, set[str]] = {
        item.game_path: set(item.state_usage_files) for item in items
    }
    active_items = [item for item in items if names_by_item[item.game_path]]
    if not active_items:
        return {item.game_path: sorted(usage[item.game_path]) for item in items}

    for text_path in text_files(mod_root):
        data = text_path.read_bytes()
        rel_path = text_path.relative_to(mod_root).as_posix()
        for item in active_items:
            if any(name in data for name in names_by_item[item.game_path]):
                usage[item.game_path].add(rel_path)
    return {game_path: sorted(paths) for game_path, paths in usage.items()}


def apply_gfx_replacements(
    items: list[DeleteItem],
    mod_root: Path,
    replacement: str,
    apply: bool,
) -> tuple[dict[str, list[dict[str, Any]]], set[Path]]:
    replacement_counts: dict[str, list[dict[str, Any]]] = {
        item.game_path: [] for item in items
    }
    changed_files: set[Path] = set()

    for gfx_path in gfx_files(mod_root):
        original = gfx_path.read_bytes()
        updated = original
        per_file_counts: list[tuple[DeleteItem, int]] = []
        for item in items:
            updated, count = replace_case_insensitive(
                updated, item.game_path, replacement
            )
            if count:
                per_file_counts.append((item, count))

        if not per_file_counts:
            continue

        rel_path = gfx_path.relative_to(mod_root).as_posix()
        for item, count in per_file_counts:
            replacement_counts[item.game_path].append(
                {"file": rel_path, "count": count}
            )

        if apply and updated != original:
            gfx_path.write_bytes(updated)
            changed_files.add(gfx_path)

    return replacement_counts, changed_files


def delete_files(
    items: list[DeleteItem], replacement_path: Path, apply: bool
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for item in items:
        exists = item.file_path.exists()
        result = {
            "game_path": item.game_path,
            "path": item.file_path.as_posix(),
            "exists": exists,
            "deleted": False,
            "reason": None,
        }
        if item.file_path == replacement_path:
            result["reason"] = "replacement_asset_not_deleted"
        elif not exists:
            result["reason"] = "already_missing"
        elif apply:
            item.file_path.unlink()
            result["deleted"] = True
        results.append(result)
    return results


def validate_items(
    items: list[DeleteItem],
    replacement: str,
    replacement_path: Path,
    allow_non_goal_paths: bool,
) -> tuple[list[DeleteItem], list[dict[str, Any]]]:
    valid: list[DeleteItem] = []
    skipped: list[dict[str, Any]] = []
    replacement_key = normalize_game_path(replacement)
    for item in items:
        key = normalize_game_path(item.game_path)
        if key == replacement_key:
            skipped.append(
                {"game_path": item.game_path, "reason": "is_replacement_asset"}
            )
        elif not allow_non_goal_paths and not key.startswith(DEFAULT_GOAL_MARKER):
            skipped.append(
                {"game_path": item.game_path, "reason": "outside_gfx_interface_goals"}
            )
        elif item.file_path == replacement_path:
            skipped.append(
                {"game_path": item.game_path, "reason": "resolved_to_replacement_asset"}
            )
        else:
            valid.append(item)
    return valid, skipped


def args_from_simple_config(
    review_state_path: str, dry_run: bool
) -> argparse.Namespace:
    return argparse.Namespace(
        review_state=Path(review_state_path),
        repo_root=None,
        mod_root=DEFAULT_MOD_ROOT,
        replacement=DEFAULT_UNKNOWN_GOAL,
        audit_out=None,
        allow_non_goal_paths=False,
        skip_sprite_usage_scan=False,
        apply=not dry_run,
    )


def main(args: argparse.Namespace | None = None) -> int:
    if args is None:
        args = parse_args()
    repo_root = (
        args.repo_root.resolve() if args.repo_root else find_repo_root(args.mod_root)
    )
    mod_root = resolve(args.mod_root, repo_root).resolve()
    review_state = args.review_state.resolve()
    audit_out = (
        args.audit_out.resolve() if args.audit_out else audit_path_for(review_state)
    )
    replacement = normalize_slashes(args.replacement)
    replacement_path = (mod_root / replacement).resolve()

    if not mod_root.is_dir():
        raise SystemExit(f"Mod root not found: {mod_root}")
    if not review_state.is_file():
        raise SystemExit(f"Review state not found: {review_state}")
    if not replacement_path.is_file():
        raise SystemExit(f"Replacement texture not found: {replacement_path}")

    raw_items, skipped = load_deleted_items(review_state, repo_root, mod_root)
    items, validation_skips = validate_items(
        raw_items, replacement, replacement_path, args.allow_non_goal_paths
    )
    skipped.extend(validation_skips)

    gfx_references = find_gfx_references(items, mod_root)
    replacement_counts, changed_files = apply_gfx_replacements(
        items, mod_root, replacement, args.apply
    )
    sprite_usage_files = (
        {} if args.skip_sprite_usage_scan else scan_sprite_usages(items, mod_root)
    )
    delete_results = delete_files(items, replacement_path, args.apply)

    audit_items = []
    for item in items:
        audit_items.append(
            {
                "game_path": item.game_path,
                "file_path": item.file_path.as_posix(),
                "file_name": item.file_name,
                "state_path": item.state_path,
                "decided_at": item.decided_at,
                "sprite_names": item.sprite_names,
                "state_usage_files": item.state_usage_files,
                "sprite_usage_files": sprite_usage_files.get(
                    item.game_path, item.state_usage_files
                ),
                "gfx_references": gfx_references.get(item.game_path, []),
                "gfx_replacements": replacement_counts.get(item.game_path, []),
            }
        )

    files_with_replacements = sorted(
        {entry["file"] for entries in replacement_counts.values() for entry in entries}
    )
    deleted_count = sum(1 for result in delete_results if result["deleted"])
    delete_candidates = sum(
        1 for result in delete_results if result["exists"] and not result["reason"]
    )
    replacement_total = sum(
        entry["count"] for entries in replacement_counts.values() for entry in entries
    )

    audit = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "apply" if args.apply else "dry-run",
        "review_state": review_state.as_posix(),
        "mod_root": mod_root.as_posix(),
        "replacement": replacement,
        "summary": {
            "delete_decisions": len(raw_items),
            "valid_items": len(items),
            "skipped_items": len(skipped),
            "gfx_files_to_edit": len(files_with_replacements),
            "gfx_replacements": replacement_total,
            "files_to_delete": delete_candidates,
            "files_deleted": deleted_count,
        },
        "changed_gfx_files": sorted(
            path.relative_to(mod_root).as_posix() for path in changed_files
        ),
        "files_with_replacements": files_with_replacements,
        "delete_results": delete_results,
        "items": audit_items,
        "skipped": skipped,
    }
    audit_out.write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")

    action = "Applied" if args.apply else "Dry run"
    print(f"{action}: {len(items)} valid delete item(s), {len(skipped)} skipped.")
    print(
        f"{'Edited' if args.apply else 'Would edit'} {len(files_with_replacements)} .gfx file(s), {replacement_total} texture reference(s)."
    )
    print(
        f"{'Deleted' if args.apply else 'Would delete'} {deleted_count if args.apply else delete_candidates} image file(s)."
    )
    print(f"Audit: {audit_out}")
    if not args.apply:
        print(
            "No files changed. Re-run with --apply to perform the edits and deletions."
        )
    return 0


if __name__ == "__main__":
    # Simple run config:
    # Paste a review_state path here, then run this file with no command-line args.
    # Keep DRY_RUN = True to audit only. Set DRY_RUN = False to edit .gfx files and delete images.
    REVIEW_STATE_PATH = r"review/goal_icon_swipe_app/goal_icon_review_state (8).json"
    DRY_RUN = False

    if len(sys.argv) == 1:
        if not REVIEW_STATE_PATH:
            raise SystemExit(
                "Paste a review_state JSON path into REVIEW_STATE_PATH, or pass it as a command-line argument."
            )
        raise SystemExit(main(args_from_simple_config(REVIEW_STATE_PATH, DRY_RUN)))
    raise SystemExit(main())
