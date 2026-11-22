# Demo script: "Pay this week's invoices"

One use case, recorded end to end in about 2 minutes. The request is the dashboard's own sample for track 6:

> "Here are this week's invoices from our accounts inbox. Match each one to its purchase order, check the vendor and its bank account, pay each one once, and hold anything unusual for my approval with the reason."

Tested end to end on 22 Nov 2026 from a clean state. Every result below is what the service returned.

## Before you record

```bash
# reset to a clean state first: see "Reset" at the end
python3 -m vendorcheck.cli serve --port 8080
```

Open http://localhost:8080 and keep `data/fixtures/bills/01_shadowfax_invoice.jpg` ready to drag in.

## Shot list

| Time | Screen | Do | Say | Verified result |
|---|---|---|---|---|
| 0:00 | Home `/` | Let the product window cycle one tab | "AP teams pay outside firms to verify vendors and pay invoices. We do the job itself." | — |
| 0:10 | Home | Click **Open the desk** | "This is the AP lead's morning." | Dashboard loads |
| 0:15 | Dashboard | Drag `01_shadowfax_invoice.jpg` onto **Scan a bill** | "A paper bill. Read on this machine; the photo never leaves it." | `V001:2026-451`, **Needs approval**, all checks passed, about 5 s |
| 0:30 | Case | Scroll checks, open **Vendor check** evidence | "Every fact is cited and dated. Crustdata by domain, never by name: three companies are called Shadowfax." | 6/6 checks pass; namesakes cleared; cost 8.2 credits ≈ ₹205 |
| 0:45 | Case | **Press and hold** to approve | "Paying is the one irreversible step, so a person holds to approve." | Paid once, ref `SIMPAY-…` |
| 0:55 | Dashboard | Click **Process inbox** | "Now the rest of this week's inbox." | 7 documents in, sorted into needs approval / held / duplicate |
| 1:05 | Payments | Open the **Duplicate** row | "The same bill came again by email, numbered INV-2026-451. Caught; not paid twice." | duplicate of `V001:2026-451` |
| 1:15 | Payments | Open **bank change request** | "A polite 'we changed banks' email. Held, and a call-back is drafted to the number on file. Never sent by the agent." | held, risk 60, draft shown |
| 1:25 | Payments | Open **INV/2026/0498** | "A look-alike domain, shadowfaxx.in. Held." | held, edit distance 1 |
| 1:30 | Payments | Open **SCM-778** | "A front company: no record carries its domain. 'Cannot verify' instead of a guess." | held, CANNOT_VERIFY |
| 1:38 | Terminal | `CRASH_AFTER_APPROVE=1 python3 -m vendorcheck.cli approve "V002:TCIE-26-27-3390" --by "AP lead"`, then `python3 -m vendorcheck.cli resume` twice | "Kill it between approval and payment." | exit 9 · state approved · 0 payments → resume pays once (`SIMPAY-66BFF274E3`) → second resume does nothing |
| 1:50 | Insights | Show **Money protected** and **Price against cost** | "₹1,500 per vendor verified against ₹205 of data. No model makes a decision." | money protected, ~91% margin |
| 2:00 | — | End | | |

Optional, if time allows: **Payments → Bank lines → Post lines** with the sample CSV. Expected: Swiggy Limited → staff meals; AWS posted, then the duplicate AWS line **held**; `PAYTM*SHARMA TRD` **cannot verify**; NEFT to Shadowfax reconciled to the approved payment (it is held if Shadowfax was not approved first).

## Reset

```bash
sqlite3 data/vendorcheck.db "DELETE FROM invoices; DELETE FROM files_seen; DELETE FROM payments; DELETE FROM events; DELETE FROM jobs; DELETE FROM steps; DELETE FROM bank_lines;"
rm -f data/fixtures/inbox/scan_*.txt
```

The response cache is kept, so a re-run is fast and costs no extra credits.
