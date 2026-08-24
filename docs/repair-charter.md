# Charter: Repair Phase — GPT-VoiceCoding v0

You are the **repair agent** for the GPT-VoiceCoding v0 repair phase. One agent, sequential
work, one ticket per branch, off `main` (origin is current at `0a0add2`). You fix; the
advisor (`gpt-voicecoding-8e`, via `SendMessage`) adjudicates every uncertain call and
Simon's standing rules override everyone. Report to Simon in Chinese; artifacts in English.

**Status: ACTIVE (unfrozen 2026-08-24).** This phase is governed by the wayfinder map
[#49](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/49) and tracked as its
ticket [#52](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/52); the E2E
suite design must be approved on ticket
[#51](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/51) before the suite
is built. Amendment history: charting session added #48 to scope, ungated #41 (RETIRE
decided), and replaced the #12 acceptance leg with Simon's manual acceptance run.

## Scope — exactly these tickets, nothing else

All on `okqixiaobao727-design/GPT-VoiceCoding`:

| order | ticket | one-line |
| --- | --- | --- |
| 1 | #32 | CI enforces a formatter it does not pin — fix FIRST, it unbreaks CI for every branch after |
| 2 | #42 + #37 | channel plugin never rendered/installed (#42) and never selected (#37) — two halves of one feature; propose to the advisor whether one branch or two BEFORE starting |
| 3 | #38 | manifest symptom resolves via #42; #38's own deliverable is the bundle self-containment check (widened per #43's note) |
| 4 | #39 | engine never attaches to a launched Codex Session's app-server |
| 5 | #40 | Claude side never raises SessionStopped (read #20 alongside — sibling, same observation point) |
| 6 | #41 | Notice Relay: **RETIRE — decided** (Simon, 2026-08-24, recorded on the ticket): remove `notice_relay` from the Agent seam and both adapters, plus `peer.py`, `notice.py`, the cc-socks receipt listener, `remove_stale_listeners` — no orphaned remainder |
| 7 | #43 | build pipeline rewrites only enumerated shebangs, not all of engine/bin/ |
| 8 | #44 | /tmp/vc-approvals-<pid>/ never removed |
| 9 | #48 | inbound Companion Channel messages leave no trace in the engine log — log the exercised path per the engine's `getLogger` convention (precedent: legacy #23) |
| 10 | #45 + #47 | mirrored constants: registry dir (Python×2) and socket path (cross-language) — one convention, precedent in `TestTheThingsThatMustAgree` (test_app_bundle.py:262); see ledger V19 |
| 11 | #46 | protocol version compared by nobody — the check is the fix; deleting the field is not on the table |

NOT in scope: #31, #33, #36 (post-v0 queue), #17/#29 (parked), any refactor not demanded by
a ticket. Scope creep goes to the advisor first, always.

## Discipline (standing law of this project — it binds)

- **Red first.** Every fix starts from the ticket's reproduction failing (or a new test
  red on today's `main`), and ends with it flipped. A fix whose test only exercises an
  injected closure repeats the gap that shipped these defects — the test must fail on the
  **production wiring** of the unfixed tree.
- **One ticket, one branch, one merge** — sequential, each reviewed before the next
  starts. Merges land as Simon directs. Deviations from a ticket's Done-when: one line
  each in the resolution comment.
- Closed sets stay closed; no dual truth; surfaces render engine words verbatim; numbers
  are derived, not picked. If a fix seems to need violating one of these, stop and ask.
- Tests/lint only via the project venv: `.venv/bin/python -m pytest -q`,
  `.venv/bin/ruff check src tests`. Swift: `swift test` in `shell/` (note #36: the
  supervision suite is red UNDER LOAD on today's main — that is a known open ticket, not
  your regression; verify on an idle machine and don't chase it).
- Evidence disputes: the audit agent `gpt-voicecoding-de` holds the full trace behind
  every finding and every verified-connected entry — ask it, don't re-derive.

## Exit criterion (the phase is over when ALL of these hold)

1. Every scope ticket closed with a resolution comment (reproduction flipped, quoted).
2. **The end-to-end integration suite exists and is green in CI**: for BOTH agent lanes,
   launch → relay delivered (proven by receipt/readback) → turn runs → approval
   round-trips → stop notice fires. Real wiring on the product side of every seam; where
   a real agent binary is infeasible in CI, the fake sits on the FAR side of the socket
   (a protocol-speaking stand-in for Claude Code / codex app-server), never inside the
   product. **Send the advisor the suite's design before building it.**
3. Bundle rebuilt from the merged tree, installed, provenance verified (byte-compare
   against HEAD, the acceptance run's `diff -r` precedent).
4. A closing summary: what changed, per ticket, and what Simon needs for his **manual
   acceptance run** — he runs acceptance himself after this phase, NOT via #12. The
   summary names the checks previously blocked (the old steps 4, 5, 6, 7, 10-remainder)
   and points at the surviving evidence from the #12 run (steps 0–3, 8/9).

## Read first

- The audit ledger: `/tmp/gpt-voicecoding-wiring-audit-report.md` — especially its
  closing note on V17 (load-bearing docstrings in this repo are sometimes TRUE — verify,
  don't assume every one is a #37) and V19 (the cheap-fix precedent for #45/#47).
- Tickets #37–#47 in full; #20, #29, #36 for boundaries.
- `CONTEXT.md`, `docs/adr/0001` (seams), `docs/app-bundle.md`.
- `/tmp/gpt-voicecoding-advisor-handoff-session7.md` for project law and context.
