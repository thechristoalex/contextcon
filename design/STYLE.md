# Payables Desk: style reference

The rules the signed-in app follows. Brand from **Baselayer**, density and tables from **Linear**. Code lives in `vendorcheck/static/app.css` (tokens + components) and `vendorcheck/ui.html` (screens).

## 1. Audit of the previous app (why it looked weak)

**Score: 42/100.** Components reviewed: 9. Issues: 14.

| Issue | Where | Fix adopted |
|---|---|---|
| Marketing layout inside a tool: big serif page titles, centred hero | every screen | Tool layout: sidebar + 48px top bar, page title 20px Inter 600 |
| Top tab nav with 8 items wraps and hides counts | header | Left sidebar grouped by job, with live counts |
| Tables at 15px with 12px padding: about 48px rows, low density | inbox, ledger, approvals | 36px rows, 13px text, hairline rules, sticky header |
| Status shown only as text chips | all tables | Status icon + label (shape and colour, readable without colour) |
| No feedback after actions (approve, process, save) | everywhere | Toasts, button loading state, optimistic row update |
| Case view is one long scroll of panels | case | Two columns: activity/checks left, properties right (Linear issue view) |
| Evidence dumped inline | case | Collapsible evidence per check |
| No keyboard support | everywhere | ⌘K palette, `g` + key navigation, `j`/`k` rows, `Enter` open |
| Hardcoded colours in markup | ui.html | Tokens only |
| Pay button identical to other buttons | case | "Hold to approve": deliberate 1.2s press for the irreversible step |

## 2. Tokens

| Group | Token | Value | Use |
|---|---|---|---|
| Canvas | `--bg` | `#ffffff` | page |
|  | `--bg-sub` | `#f7f8fa` | sidebar, table header, hover |
|  | `--bg-sel` | `#eef0fd` | selected row |
| Ink | `--fg` | `#0b0f17` | text |
|  | `--fg-2` | `#4b5263` | secondary text |
|  | `--fg-3` | `#8a90a0` | tertiary, placeholders |
| Rules | `--line` | `#ebedf1` | hairlines |
|  | `--line-2` | `#dfe2e8` | control borders |
| Brand | `--navy` | `#09234f` | primary button, system fields |
|  | `--blue` | `#384ce3` | live signal, focus, selection |
| Status | `--paid` | `#1f8a63` | paid / pass |
|  | `--wait` | `#384ce3` | awaiting approval |
|  | `--held` | `#c27a12` | held / warn |
|  | `--bad` | `#c03a2b` | duplicate / rejected / fail |
|  | `--muted` | `#8a90a0` | not run |
| Type | `--sans` | Inter 400/500/600 | UI |
|  | `--mono` | IBM Plex Mono 500 | ids, amounts, keys, labels |
| Size | 11 / 12 / 13 / 14 / 20 px | | label / meta / body / emphasis / page title |
| Space | 4px base: 4 8 12 16 24 32 | | |
| Radius | `--r` 4px (controls) · 8px (palette, toasts) · 0 (tables, panels) | | |
| Elevation | none on surfaces; `--shadow-pop` only on palette, toast, menu | | |
| Motion | `--fast` 120ms · `--base` 180ms · `--slow` 260ms; ease `cubic-bezier(.2,.8,.2,1)` | | |

## 3. Components

| Component | Variants | States | Notes |
|---|---|---|---|
| Button | primary (navy), accent (blue), ghost, danger | hover (bg step), active (scale .98), focus ring 2px blue, loading (spinner, label kept), disabled | 28px high, 12px x-padding, 13px |
| Hold button | money | idle → holding (fill grows left→right over 1.2s) → done (check) ; release early cancels | only for payment approval |
| Status icon | paid ● filled, awaiting ◐ half, held ◔ quarter, duplicate ⊘, rejected ✕, running spinner | tooltip with label | 14px SVG, never colour alone |
| Table | — | row hover, row selected (`j`/`k`), sticky header | 36px rows, numeric columns right-aligned, tabular figures |
| Filter chips | — | active, count badge | sit above tables |
| Sidebar item | — | hover, active (blue left marker + bg), count | grouped: Work / Records / Tools / System |
| Properties panel | — | copyable value (click → "Copied") | right column of case view |
| Check row | pass / warn / fail / unavailable | collapsed, expanded (evidence) | vertical rail connects checks |
| Toast | success / error / info | enter (slide up + fade), auto-dismiss 3.2s | bottom-right |
| Command palette | — | open (⌘K / Ctrl-K), filtered, active item | fuzzy over screens and documents |
| Skeleton | — | shimmer | first load of every screen |
| Empty state | — | — | one sentence + the action that fills it |

## 4. Microinteractions

1. **Hold to approve** a payment: the irreversible step needs a deliberate press; it also makes the demo legible.
2. **Optimistic status change**: an approved row flips to *Paid* with a short pulse, then the server confirms.
3. **Count-up** numbers on the dashboard on first load.
4. **Copy** idempotency keys, refs and invoice numbers with one click; label changes to *Copied* for 1.2s.
5. **Keyboard**: ⌘K palette; `g d` dashboard, `g i` inbox, `g a` approvals, `g v` vendors, `g l` ledger; `j`/`k` move, `Enter` opens, `Esc` back.
6. **Row enter**: new rows fade in staggered 20ms.
7. **Expandable evidence** with a rotating chevron.
8. **Skeleton** rows while data loads; no layout jump.
9. All motion off under `prefers-reduced-motion`.

## 5. Screens

| Screen | Job | Layout | Key interactions |
|---|---|---|---|
| Dashboard | What needs me now, and is the desk healthy | 4 KPIs, "Needs you" list, recent activity, spend | count-up, click-through |
| Inbox | Every document and its outcome | filter chips (All · Needs approval · Held · Paid · Duplicate), table | `j`/`k`, Enter, drop a document |
| Case | Why this decision, and act on it | left: decision banner, checks rail with evidence, draft message, audit trail; right: properties | hold-to-approve, replay checks, copy keys |
| Approvals | The queue a person works through | table of awaiting, table of held | open, approve from case |
| Vendors | The vendor master and trust verdicts | table, verdict icon, verified account | Check now |
| Vendor check | Check one company on its own | form → verdict banner, facts table, flags, cannot-verify | loading state, cost shown |
| Bank lines | Post a statement | paste CSV → results table | duplicate and cannot-verify rows highlighted |
| Ledger | Money that moved | table, total row | copy ref |
| Activity | Everything the agent did | dense log, level icons | filter by text |
| Settings | Limits and approval policy | form, saved toast | change logged with identity |
