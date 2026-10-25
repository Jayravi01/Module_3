"""Line-level diff between two normalised page snapshots.

Only the changed lines (plus a little context) are sent to the LLM. This keeps
cost low and focuses the model on what actually changed, which improves
classification accuracy compared with sending the whole page.
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass


@dataclass
class PageDiff:
    added: list[str]
    removed: list[str]
    context: str            # unified-diff excerpt sent to the LLM
    change_ratio: float     # 0..1, share of lines that changed

    @property
    def is_trivial(self) -> bool:
        """Ignore diffs that are only tiny cosmetic edits."""
        changed = " ".join(self.added + self.removed)
        return len(changed.strip()) < 15


def diff_pages(old: str, new: str, max_chars: int = 12000) -> PageDiff:
    old_lines, new_lines = old.splitlines(), new.splitlines()
    added, removed = [], []
    for line in difflib.ndiff(old_lines, new_lines):
        if line.startswith("+ "):
            added.append(line[2:])
        elif line.startswith("- "):
            removed.append(line[2:])

    unified = "\n".join(difflib.unified_diff(old_lines, new_lines, "previous", "today",
                                             n=2, lineterm=""))
    if len(unified) > max_chars:
        unified = unified[:max_chars] + "\n... [diff truncated]"

    total = max(len(old_lines), len(new_lines), 1)
    ratio = min(1.0, (len(added) + len(removed)) / (2 * total))
    return PageDiff(added, removed, unified, round(ratio, 3))
