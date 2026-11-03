r"""Re-apply the CURRENT baseline rules to baseline records stored by an older version.

    python scripts\retag_baselines.py            # preview only: shows what would change
    python scripts\retag_baselines.py --apply    # write the changes

Why: baselines stored before milestone 3.3 were tagged with every visa subclass mentioned
anywhere on the page (e.g. the Student 500 page as "500, 590, 485 ...") and with keyword
change types such as "processing_time", and had no basis values. Since 3.3 a baseline is:
    visa_categories = the page's own visa (visa_hint in config/sources.yaml)
    visa_basis      = stated (all of them named on that day's page) / inferred / none
    change_type     = 'baseline', impact Low, impact_basis and effective_date_basis 'none'
Only rows with record_type = 'baseline' are touched; changes are never modified. Safe to run
again: rows that already follow the rules are reported as unchanged.
"""
import argparse
import json

import _bootstrap
from visa_monitor.classifier import find_subclasses


def as_list(value):
    """Arrays are lists in PostgreSQL and JSON text in SQLite."""
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else json.loads(value or "[]")


def plan(store):
    """List of (record_id, source name, old values, new values) for baselines that differ."""
    rows = store.query(
        "select r.id, s.name, s.visa_hint, r.visa_categories, r.change_type, r.visa_basis, "
        "r.effective_date_basis, r.impact_basis, r.impact_level, p.content "
        "from regulatory_records r join sources s on s.id = r.source_id "
        "left join page_snapshots p on p.source_id = r.source_id and p.snapshot_date = r.record_date "
        "where r.record_type = 'baseline' order by r.id")
    out = []
    for rid, name, hint, visas, ctype, vbasis, ebasis, ibasis, level, content in rows:
        hints = [str(h) for h in as_list(hint)]
        named = find_subclasses(content or "")
        new_vbasis = "none" if not hints else ("stated" if all(h in named for h in hints) else "inferred")
        old = (as_list(visas), ctype, vbasis, ebasis, ibasis, level)
        new = (hints, "baseline", new_vbasis, "none", "none", "Low")
        if old != new:
            out.append((rid, name, old, new))
    return out


def main():
    parser = argparse.ArgumentParser(description="Re-apply the current baseline rules to stored baselines.")
    parser.add_argument("--apply", action="store_true", help="write the changes (default: preview only)")
    args = parser.parse_args()
    store = _bootstrap.open_store()
    changes = plan(store)
    print(f"{len(changes)} baseline record(s) differ from the current rules"
          + ("" if args.apply else " (preview only, nothing written)"))
    for rid, name, old, new in changes:
        print(f"  #{rid} {name[:45]:<45} visa {', '.join(old[0]) or '-'} -> {', '.join(new[0]) or 'General'} "
              f"({new[2]}); type {old[1]} -> baseline")
    if args.apply and changes:
        for rid, _name, _old, new in changes:
            store._exec("update regulatory_records set visa_categories=?, change_type=?, visa_basis=?, "
                        "effective_date_basis=?, impact_basis=?, impact_level=?, effective_date=null "
                        "where id=? and record_type='baseline'",
                        (store._arr(new[0]), new[1], new[2], new[3], new[4], new[5], rid))
        store.commit()
        print(f"Updated {len(changes)} record(s). Run again to confirm: it should report 0.")


if __name__ == "__main__":
    main()
