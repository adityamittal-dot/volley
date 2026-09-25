"""volley CLI.

Routine engine (default):
  python -m volley prepare             Actions: sync, follow-ups, discover, crawl -> work/<date>/batch-N.json
  python -m volley brief               routine: what's in today's batch
  python -m volley shortlist           routine: leads to write, with contact + tailoring eligibility
  python -m volley check               routine: must pass before pushing (lint + truth checks)
  python -m volley ingest [date]       Actions: validate, render resumes, queue, deliver this window
  python -m volley deliver             Actions: sync + deliver queued emails for this window
  python -m volley test-send <to> <case>   run one test case end to end, send it to <to>

API engine (needs ANTHROPIC_API_KEY):
  python -m volley run                 daily run (sync, follow-ups, discover, write, deliver)
  python -m volley run --dry-run       everything except touching Gmail; writes data/preview.md
  python -m volley discover            fetch + score leads only, print the top 20
  python -m volley status              counts, spend, replies
  python -m volley mark <company> <status> [note]   e.g. mark acme interview "call on Tue"
"""
import argparse

from . import llm, pipeline, routine, store
from .config import profile, settings


def main():
    ap = argparse.ArgumentParser(prog="volley")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--all-regions", action="store_true", help="ignore send windows")
    r.add_argument("--no-discover", action="store_true")
    for name in ("prepare", "brief", "shortlist", "check"):
        sub.add_parser(name)
    i = sub.add_parser("ingest")
    i.add_argument("date", nargs="?")
    d = sub.add_parser("deliver")
    d.add_argument("--all-regions", action="store_true")
    t = sub.add_parser("test-send")
    t.add_argument("to")
    t.add_argument("case", help="folder with email.json (+ resume.json), e.g. test/vidyalai")
    sub.add_parser("discover")
    sub.add_parser("status")
    m = sub.add_parser("mark")
    m.add_argument("company")
    m.add_argument("status", choices=store.STATUSES)
    m.add_argument("note", nargs="?", default="")
    args = ap.parse_args()

    if args.cmd == "prepare":
        routine.prepare()
    elif args.cmd == "brief":
        routine.brief()
    elif args.cmd == "shortlist":
        rows = routine.shortlist()
        print(f"\n{len(rows)} leads to write. Tailoring-eligible: {sum(r['tailor_eligible'] for r in rows)}")
    elif args.cmd == "check":
        problems = routine.check()
        print("CHECK PASSED" if not problems else "CHECK FAILED:\n  " + "\n  ".join(problems))
        raise SystemExit(1 if problems else 0)
    elif args.cmd == "ingest":
        routine.ingest(args.date)
    elif args.cmd == "deliver":
        routine.deliver(args.all_regions)
    elif args.cmd == "test-send":
        routine.test_send(args.to, args.case)
    elif args.cmd == "run":
        pipeline.run(dry_run=args.dry_run, all_regions=args.all_regions, no_discover=args.no_discover)
    elif args.cmd == "discover":
        leads = store.load()
        pipeline.discover(leads, settings(), profile())
        store.save(leads)
        top = sorted((l for l in leads.values() if l["status"] == "new"), key=lambda l: l["score"], reverse=True)
        for l in top[:20]:
            print(f"{l['score']:>5}  {l['company'][:30]:30}  {l['title'][:45]:45}  {l['source']}")
    elif args.cmd == "status":
        leads = store.load()
        store.export(leads)
        print(open(store.ROOT / "TRACKER.md").read().split("## Latest")[0])
        print(f"spent today: ${llm.spent_today():.3f}")
    elif args.cmd == "mark":
        leads = store.load()
        hits = [l for l in leads.values() if args.company.lower() in l["company"].lower()]
        if len(hits) != 1:
            raise SystemExit(f"{len(hits)} matches: {[l['company'] for l in hits][:10]}")
        hits[0]["status"] = args.status
        if args.note:
            hits[0]["note"] = args.note
        store.save(leads)
        store.export(leads)
        print(f"{hits[0]['company']} -> {args.status}")


if __name__ == "__main__":
    main()
