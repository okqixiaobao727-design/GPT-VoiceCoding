# 16. Progress publishes one observation as a roster summary or exact detail

Date: 2026-08-30 · Status: Accepted · Source: [#76](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/76), whose Advisor Q2 bound is superseded by Simon on 2026-08-30

Issue #76 made one `Progress` value serve three jobs: the normalized fact read from
Claude or Codex, the compact value stored on every Session roster row, and the exact
answer to a user's `progress` request. Its Advisor Q2 ruling therefore imposed one
three-entry, 3 KB encoded bound on all three. The implementation applies that bound
inside the Agent adapters before Bridge Core knows which answer will carry it
(`adapters/agent/_progress.py:1-60`).

That coupling produced a false statement in the Live Call. A real Codex Session had
visible history and a newest assistant entry whose encoded wire value was 5,482 bytes.
The 3,072-byte bound dropped the whole entry and returned `recent=()` with
`truncated=true`; the shared command renderer then printed both "older entries
dropped" and "nothing said yet" (`control_plane/commands.py:205-226`). The Session was
paused, its rollout remained complete, and two later short messages made progress
visible again. No history had been lost. A transport projection had destroyed the
distinction between *read and empty* and *read, but omitted*.

Raising 3 KB is not a proof. The bound rides on every Session in one status reply,
while the protocol limits the entire line to 65,536 bytes. The existing test proves
only `3 KB * 10 < 64 KB` (`tests/test_progress_bound.py:102-107`); the affected engine
was watching 38 Codex Sessions. Even before another field is counted, a fixed per-row
allowance cannot establish the whole-reply invariant for an unbounded roster.

## Decision

**There is one canonical progress observation and two publications.** Claude and
Codex still read only their own authoritative source and normalize it into the same
Agent-seam vocabulary. That observation is a fact, not a pre-rendered roster row. A
status or sessions answer publishes a summary; an exact progress answer publishes
detail. Different publications may carry different amounts of the same observation,
but they may not disagree about availability, history presence, role, order, text or
read time.

The observation model makes the states that the old empty tuple collapsed explicit:

- `not_read`: no source read was made. It is not empty history.
- `unreadable`: the source could not answer. An exact progress action maps this to its
  existing typed refusal; a roster retains the last observed fact and reports the lane
  degradation under the existing registry rules.
- `readable`: the source answered. It carries `has_history`, a whole-entry ordered
  tail, its source omission, and `read_at`.

For a readable observation, `has_history=false` is the only fact that means "nothing
said yet". It requires an empty tail and no omission. `has_history=true` remains true
when no entry could be captured or published. Omission is named rather than inferred:
`none`, `older`, `status_summary`, or `newest_oversize`. If the compatibility field
`truncated` remains on the wire during migration, it is derived from omission and is
never again the source of meaning.

**`status` and `sessions` publish no chat body.** Their Session rows carry only the
progress summary: availability, history presence, omission and read time. The current
`bridgectl` roster renders no progress text (`control_plane/commands.py:193-202`), and
the current Swift `SessionRow` reads none (`shell/Sources/ShellCore/ControlPanel.swift:97-134`).
Transporting every stopped Session's chat body therefore spends the shared response
budget without serving either current roster consumer. A readable Session with
history is published as `has_history=true`, `recent=[]`, and
`omission=status_summary`; it is never published as empty history.

**An exact `progress` action owns the reply's remaining capacity.** Bridge Core still
resolves one exact target, calls that lane's `inspect` once, starts no turn, and folds
the resulting observation back into the roster before answering. A private in-process
`ProgressPublication` module then uses the final Reply shape and its canonical UTF-8
JSON-line encoder to calculate the bytes left after the protocol envelope and Session
fields. It keeps the newest complete entry and widens backwards while the complete
reply still fits. Entries remain whole or omitted; this decision does not permit
cutting a message.

The source capture ceiling is derived from the largest registered publication capacity,
not chosen independently by either adapter. Content that cannot fit any registered
publication is retained as `has_history=true` plus `newest_oversize`, without its text.
Adapters know only the capture capacity supplied at composition; they do not select a
roster budget, an exact-progress budget or a presentation profile.

**The 65,536-byte limit applies to the encoded Reply, not to `recent`.** The same
encoder measures and writes the line, including the protocol envelope, action, JSON
escaping and terminating newline. Before writing, the Control Plane performs one final
capacity check as defence in depth. If even the status skeleton with every progress
body removed is too large, the engine returns a bounded refusal. It neither writes an
over-limit reply nor silently removes Session rows. Pagination or Session-row omission
would be a separate product decision.

**The Control Plane protocol becomes version 5 when this decision is implemented.**
Protocol 4 promises that `status`, `sessions` and `progress` carry the same Session-row
shape, with one three-entry, 3 KB progress value (`docs/control-plane.md`). Version 5
removes chat bodies from roster publications, gives exact progress a detail publication,
and makes omission distinguishable from empty history. An older surface can otherwise
render the new honest omission as the old false "nothing said yet", so this is not an
additive version-4 field change.

`ProgressPublication` is a private deep module, not a new external seam. The two Agent
adapters remain the only varying source implementations. The module hides source and
view omission composition, exact wire measurement, whole-entry tail selection, status
summary rendering, and final-capacity validation. Callers choose only the existing
`status`/`sessions` or `progress` action; no caller passes entry counts, per-row limits,
fairness rules or byte estimates.

## Legacy classification

The exact one-Session read, source ownership, no-turn rule, whole-entry selection and
final reply-size check are **adapted** from legacy. Legacy read the exact Codex thread
without resuming it (`legacy@1d32845:bridge/codex.py:1319-1348`), rejected a newest
Codex message that could not fit its configured fragment
(`legacy@1d32845:bridge/codex.py:1427-1439`), and rejected an exact progress reply that
exceeded the protocol limit (`legacy@1d32845:bridge/daemon.py:2246-2271`). Its shared
progress limits were 12 messages and 32 KB
(`legacy@1d32845:config.plist:447-452`).

This decision adapts those behaviours by deriving capacity from the complete reply and
by preserving the existence of oversize history as structured evidence. It drops the
fixed 12/32 KB and current 3/3 KB values. Legacy's empty-and-truncated oversize fallback
was explicitly for Stop Detail, where the progress fragment was optional context
(`legacy@1d32845:bridge/transcript.py:256-267,320-330`); applying that fallback to the
exact progress action was not a port.

## Consequences

A 5,482-byte newest entry is returned whole by exact progress whenever the complete
reply fits. If a newest entry cannot fit even an otherwise empty exact reply, the action
answers successfully with `has_history=true`, no text, and
`omission=newest_oversize`; every surface says that history exists but was not carried.
Only a readable observation with `has_history=false` may say "nothing said yet".

Status size no longer grows with the length of Session chat bodies. It still grows with
the number and fixed facts of Session rows, pending work and lane information, so the
whole-reply guard remains necessary. The old ten-Session multiplication test is removed;
the replacement proves the public encoded replies, including at least 38 Sessions.

The architecture preserves one live per-target read and one source of truth. Exact
progress can be more detailed than the following status reply because status publishes
a summary, but both derive from the same folded observation and carry the same
`has_history`, omission ancestry and `read_at` facts.

The implementation brief must prove through public interfaces:

- a 5,482-byte newest entry is returned whole by exact progress;
- a newest entry larger than exact reply capacity is reported as existing but omitted;
- truly empty, not read and unreadable remain distinct through every renderer;
- 38 Sessions with large histories produce a status reply no larger than 65,536 bytes;
- a status skeleton that cannot fit yields a bounded refusal and loses no Session row;
- Unicode and JSON escaping are measured as actual wire bytes;
- Claude and Codex produce the same observation and publication semantics; and
- exact progress performs one `inspect`, no Relay and no new turn, then folds that
  observation back into the roster.

## Rejected alternatives

**Raising the 3 KB constant** fixes only the measured sample. It leaves omission
indistinguishable from empty history and increases the unproven aggregate status cost.

**Dynamically sharing chat-body capacity across status rows** can satisfy the byte
ceiling, but it introduces a fairness policy and repeated final-document measurement
for content no current roster surface renders. If a future roster product needs chat
previews, that is a new publication with an explicit consumer, not a hidden cost on
every status answer.

**Cutting message text to fit** is rejected. A partial sentence changes what the Session
said and can cut at a secret or structural boundary. The existing whole-or-omitted rule
stands; what changes is that omission is represented honestly.

## Amendment 2026-09-01: a third publication, the History page

Source: [#171](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/171), under map [#164](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/164).

The 0901 flow asks for history the user can page through by voice — "the last five,
then the five before those". The exact `progress` publication above cannot answer it: it
widens backwards from the newest entry until the Reply is full and has no way to say
"the ones before that". Its byte ceiling had become the page.

**There is still one canonical observation; it now has three publications.** The roster
summary and the exact detail stand as decided. The third is the **History page**:

- **Bounded by a count, not by bytes.** A page holds `history_page_entries` entries
  (`[policy]`, default 5), both roles counted, newest first. The 65,536-byte Reply
  limit remains a ceiling on the encoded line — an entry that would push the page past
  it is published as *existing but omitted* (`ordinal`, `role`, `omission=oversize`, no
  text) and still occupies its slot, so a page always advances and an oversize entry
  never blocks the ones before it. Message text is never cut; the whole-or-omitted rule
  stands.
- **The cursor is the entry's ordinal in the Session's visible record**, counted from
  the oldest entry, assigned by the lane at read time. Both sources are append-only
  for the entries this seam keeps (a Claude transcript file; a Codex thread's turns),
  so an ordinal names the same entry across reads while the Session lives. A page
  carries each entry's ordinal and `older: bool`; the next request passes the smallest
  ordinal it received as `before`.
- **A separate read, never folded into the roster.** `inspect` keeps answering the
  newest tail and folding into the roster; a History page is read by its own Agent-seam
  verb and is not a roster fact. A Session the lane cannot read answers with the same
  refusals the exact publication uses; a page past the oldest entry is empty with
  `older=false`, which is an answer, not a refusal.

The exact `progress` publication and action are **retired** by this amendment: the
Session Brief carries the newest entry whole (#166), and the History page carries
everything before it. `sessions` retires with it — the Roster Brief is that surface.
Removing two actions and adding `brief` and `history` changes the Control Plane action
set, so the protocol version moves again when this lands.

**Legacy.** Legacy's `progress` was a fixed tail of 12 entries / 32 KB with a boolean
`truncated` (`legacy@1d32845:config.plist:447-452`, `bridge/transcript.py:2841`) and no
cursor of any kind; `overview` took no arguments (`bridge/daemon.py:1552`). **Legacy has
no paging behaviour** — the count-bounded page and the ordinal cursor are new; the
whole-entry rule and the exact one-Session read remain adapted as above.
