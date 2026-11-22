# Demo script: "A paper bill, paid once" (about 2 minutes)

One use case, start to finish: three paper bills from two real Indian logistics vendors. One is paid once, one is a duplicate, and one tries to divert the money. A crash in the middle proves exactly-once payment.

Tested end to end on 22 Nov 2026 with the photos in `data/fixtures/bills/`.

## Before recording (1 minute)

```bash
sh scripts/demo_reset.sh
python3 -m vendorcheck.cli serve --port 8080
```

Open http://localhost:8080 in a browser window sized about 1440×900, and keep a terminal visible for step 6.

## The run

| # | Time | Do this | Say this | What appears (tested) |
|---|---|---|---|---|
| 1 | 0:00 | Home page, hover the product window | "Payables teams lose money to duplicate invoices, look-alike vendors and bank-detail fraud. This desk proves every vendor first and pays every invoice once." | Tabs replay the four test cases |
| 2 | 0:12 | Click **Open the desk** | "Here's the AP lead's morning: what needs them today." | Dashboard: greeting, Scan a bill, cards, Today list |
| 3 | 0:20 | **Scan a bill → Upload** `data/fixtures/bills/01_shadowfax_invoice.jpg` | "A paper bill from Shadowfax. It's read on this machine; the photo never leaves it." | Case opens: **Ready to pay. All checks passed**, with the photo and field boxes |
| 4 | 0:35 | Scroll the checks; open **Vendor check** | "Every check cites its source: the vendor resolved by domain on Crustdata, the website, directors screened, namesakes cleared, the PO and goods receipt matched, the bank account verified at onboarding. 8 credits, about ₹205." | Checks list with dated sources |
| 5 | 0:55 | Back to Dashboard, upload `04_shadowfax_invoice_resent.jpg` | "The vendor re-sends the same bill as a PDF with a different number format." | **Duplicate. Not paid**: INV-2026-451 = INV/2026/0451 |
| 6 | 1:05 | Upload `06_tci_invoice_new_account.jpg` | "TCI Express asks to be paid into a new account." | **Held**: account differs from the verified one; call-back drafted to the number on file, never sent |
| 7 | 1:20 | Terminal: `CRASH_AFTER_APPROVE=1 python3 -m vendorcheck.cli approve "V001:2026-451" --by "AP lead"` | "A person approves the clean bill, and I kill the process before the payment goes out." | Process exits; invoice **approved**, 0 payments |
| 8 | 1:30 | Terminal: `python3 -m vendorcheck.cli resume` (run it twice) | "Restart. It finishes the payment once. Running it again does nothing." | First: `"ok": true, "ref": "SIMPAY-D0D30AD9B5"`. Second: `[]` |
| 9 | 1:40 | **Payments → Ledger**, then **Insights** | "One row per invoice, keyed by the invoice. Insights shows money stopped and our margin: ₹1,500 per vendor against ₹205 of data." | Ledger: ₹2,36,000 paid once. Insights: money protected, price against cost |
| 10 | 1:55 | Close | "Every vendor proven first. Every invoice paid once." | |

Approving in the browser also works: open the clean bill and **hold** the approve button for a second. Use the terminal path in step 7 only to show the crash.

## Recording with Cap

```bash
cap doctor --json                       # captureReady must be true (grant Screen Recording to Cap first)
cap targets --json                      # pick the screen id
cap record start --screen <id> --detach --json
# ... run the demo ...
cap record stop --id <recordingId> --json   # recordingMetaExists must be true
cap export <path.cap> --output demo.mp4 --json
```

Uploading to Cap (`cap upload demo.mp4 --json`) shares it; do that only when you're happy with the take.
