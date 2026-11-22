"""Command line.

  python -m vendorcheck.cli inbox                     # process every invoice in data/fixtures/inbox (resumes first)
  python -m vendorcheck.cli file path/to/invoice.txt  # process one document
  python -m vendorcheck.cli approve <invoice_key> --by "Priya (AP lead)"
  python -m vendorcheck.cli resume                    # pay anything approved but unpaid (after a crash)
  python -m vendorcheck.cli vendor --name "Shadowfax" --website shadowfax.in [--director "..."]
  python -m vendorcheck.cli status | payments | ledger <job_id>
  python -m vendorcheck.cli serve --port 8080
"""
import argparse
import json

from . import ap, pipeline, store


def main(argv=None):
    p = argparse.ArgumentParser(prog="vendorcheck")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("inbox").add_argument("--folder")
    sub.add_parser("file").add_argument("path")
    a = sub.add_parser("approve"); a.add_argument("key"); a.add_argument("--by", required=True)
    sub.add_parser("resume")
    v = sub.add_parser("vendor")
    v.add_argument("--name"); v.add_argument("--website"); v.add_argument("--director"); v.add_argument("--gstin"); v.add_argument("--request-id")
    sub.add_parser("status"); sub.add_parser("payments")
    sub.add_parser("ledger").add_argument("job_id")
    sub.add_parser("serve").add_argument("--port", type=int, default=8080)
    x = p.parse_args(argv)

    if x.cmd == "inbox":
        out = ap.process_inbox(x.folder)
    elif x.cmd == "file":
        out = ap.process_file(x.path)
    elif x.cmd == "approve":
        out = ap.approve(x.key, x.by)
    elif x.cmd == "resume":
        out = ap.resume()
    elif x.cmd == "vendor":
        out = pipeline.run_request({"vendor_name": x.name, "website": x.website, "director": x.director, "gstin": x.gstin, "request_id": x.request_id})
    elif x.cmd == "status":
        out = [{k: r[k] for k in ("invoice_key", "source_file", "state")} | {"reason": json.loads(r["decision_json"] or "{}").get("reason", "")[:140]} for r in ap.invoices()]
    elif x.cmd == "payments":
        out = ap.payments()
    elif x.cmd == "ledger":
        out = store.ledger_for(x.job_id)
    elif x.cmd == "serve":
        from . import server
        return server.serve(x.port)
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
