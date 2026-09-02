# 20. A Codex Session is a daemon-held user root that a live terminal vouches for

Date: 2026-09-02 · Status: Accepted · Source: [#201](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/201)

ADR 0016's roster has two Codex sources, and the rule that merges them was written five times — [#112](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/112), [#113](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/113), [#123](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/123), [#144](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/144) and now this — while living inline across five branches of one I/O function, unnamed and with no test surface of its own.

#144 settled it as: **a loaded root is a roster row only when a live interactive process carries the same exact thread id as a native daemon thread or rollout.** That killed #123's ghost rows and #144's own duplicate rows, and it did so by requiring a shared key that only `codex resume <UUID>` puts in an argv. No other invocation does — not a bare `codex`, not `codex "<prompt>"`, not `--last`, not the picker. So no hand-started Codex Session could ever reach the roster, and the acceptance harness's codex lane failed at its `roster` step in four consecutive real-machine runs (`20260902T002414Z`, `20260902T005111Z`, `20260902T005521Z`, `20260902T013516Z`), skipping every step behind it. The Codex lane was inert for the product's core promise ([#68](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/68)): a bridge over Sessions the *user* starts.

## The two measurements that force the shape

Taken on 2026-09-02 against `codex-cli 0.152.1` with the managed app-server at `0.149.1`.

1. **The daemon cannot report liveness.** A sweep of the app-server v2 protocol finds no way to name the OS process attached to a thread. `osPid` belongs to a thread's *background terminal*; `clientId` belongs to remote-control clients and to individual items; `canAcceptDirectInput` is documented as "whether the app server accepts direct turn input for this loaded thread" — a property of the server, not evidence of an attached terminal.
2. **"Loaded" is not "live."** After its TUI exited, `01a05fc1-b5ca-…` remained in `thread/loaded/list` with `status: idle` for **over thirty minutes** before flipping to `notLoaded`. Any rule that reads loadedness as liveness reinstates #123.

Identity can therefore only come from the daemon, and liveness can only come from the process table. Neither source can supply the other's half, and no third source exists.

## Decision

**A Codex Session is a daemon-held user root thread that a live terminal in its own workspace vouches for.**

- The **daemon** supplies identity and content: the thread id — which *is* the row's identity — its name, its state, its tree links, its progress.
- The **process table** supplies liveness and place only: a live interactive `codex` with a controlling terminal, and its working directory compared by realpath.
- A user root no live terminal vouches for is not a row, however recently the daemon loaded it. A terminal that vouches for no thread is not a row either: process-only evidence still never becomes a Session.

**The one row the daemon does not supply, and why it is not an exception to the sentence above.** A TUI started while the daemon was down is never adopted by a daemon that starts later (#82, measured), so such a Session is invisible to the authority. It composes a row when — and only when — its argv carries a canonical thread id and the rollout that id names, on disk, states the same id, the same real workspace and `thread_source: user`. That is native Codex evidence of a user root, written by codex itself; it is not the process table vouching for identity, and no count, timestamp or workspace-only observation can manufacture it (#144, carried forward unchanged). A terminal with no such rollout still never becomes a Session.

**#144 is superseded in exactly one clause.** Everything else it decided stands: a row's identity is always the daemon's thread id; two observations of one Session never become two rows; a pid is reported only when exactly one terminal can be it and is otherwise absent; **workspace never determines identity**. What changes is that workspace now determines *liveness*.

**The ghost stays dead by eliminating impossibilities, not by guessing.** A terminal vouches only for a thread it *could* be attached to: a thread created before the terminal's own start time cannot be that terminal's, unless the terminal's argv names it (`resume`, which is already an exact match). This removes candidates; it never manufactures a shared key, so #144's ban on timestamps-as-identity stands. Checked against the evidence: in run `20260902T013516Z` the thread's `createdAt` is 13:35:25 and the terminal started at 13:35:16, so the true thread survives the filter, while the previous run's root (created 13:19:46) is excluded.

**The comparison allows one second, and that number is read rather than chosen.** A start time is computed from `ps`'s `etime`, which is truncated whole seconds subtracted from a clock read after `ps` sampled it, so the computed start is never earlier than the true start and can be up to a second later (`processes.py`, `START_TIME_RESOLUTION_SECONDS`). Without the allowance, a TUI that opens its thread in the same second it launches — the fast start, which is the ordinary one — would lose its own row.

**`etime` rather than `lstart`.** `lstart` is an absolute time but a locale-formatted one: the machine of record prints `Wed  2 Sep 08:52:06 2026` where an en_US machine prints `Wed Sep  2 08:52:06 2026`. `etime`'s `[[dd-]hh:]mm:ss` is POSIX's own spelling and does not make the roster depend on the locale of whoever's launchd started the engine.

**`notLoaded` becomes a real state.** It is the fourth `status.type` the protocol defines and the only one that is not a state a row can be in. A thread reading it is not live and gets no row; before this it fell through to the status map's default and read as a Session mid-turn.

**Every drop leaves a stated reason.** A daemon-held user root that does not become a row is returned with why, on the principle the errand filter already follows — the reason is carried back rather than a bare `False`, because a row that stops appearing is a row somebody comes looking for. The absence of that reason is why this bug's first diagnosis was wrong: the issue asked whether the shared daemon held the Session, and it did. The engine log printed only the threads dropped as the daemon's own errands, so the user root's absence from the log read like a daemon that never offered it, while in fact it passed the errand filter and was dropped later, at row composition, silently.

**The rule is one pure function in a module of its own** (`adapters/agent/codex/roster.py`), and it is the *whole* rule: the errand filter that drops the daemon's own threads (#112) moved into it too, because a caller that filtered first would be a second copy of the rule and this function could not tell a filtered list from an unfiltered one. It takes the threads the daemon holds, exactly as they came back, and the live interactive `codex` runs, each carrying pid, workspace and start time; it returns the lane's rows, its drops and its degradation note. It performs no I/O, holds no daemon client and reads no clock. `discovery.discover` is reduced to the readings plus one call into it, and the two things that cost I/O and so cannot be decided there — each row's progress and its Session Name. This placement is as much the decision as the rule is: behind one pure function the rule is testable with dictionaries in and rows out, no fake app-server and no fake process table, which is what was missing while a wrong rule survived three tickets.

## Consequences

**`codex --last` and the interactive picker get no row.** They resume an older thread with no id in argv, and the thread predates the terminal, so nothing can vouch for it. This roster may under-report; it must never invent. Making those rosterable is its own ticket, not a reopening of this one.

**The under-reporting is stated, not silent.** When a live terminal vouches for nothing while its workspace holds daemon user roots, the lane says so through `LaneDiscovery.degraded`, beside whatever else that reading has to say. This is the condition on which the gap above was accepted.

**A terminal that could be sitting in either of two roots vouches for neither.** At most one of them is the Session it holds, and choosing would be inventing a row, so both are dropped with that reason and the lane reports the terminal through its degradation note. This is #144's refusal — a pid is named only when exactly one terminal can be it — said the other way round, and it is the boundary of workspace-as-liveness: place narrows the candidates, and where it does not narrow them to one, it decides nothing. An argv thread id names exactly one thread and is never ambiguous.

**Legacy citation** (ADR 0010). `legacy@1d32845:bridge/daemon.py:1192-1257` — **dropped, because** generation 1 only ever knew Sessions it had launched and wrapped itself, from its own launch records, so it never had to recognise one a person started. v1.0's promise (#68) is the opposite, and there is no legacy behaviour to port for it. The one habit that carries over is `pgrep` on an exact executable name (`legacy@1d32845:bridge/host.py:795`), which the process reader already does.

**What this decision refuses.** Wrapping, launching, configuring or otherwise instrumenting the user's `codex` so that it would carry an identifier is out of scope permanently: the product does not own the user's Sessions (#68, [#83](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/83), ADR 0012), and that route is exactly what generation 1 did and what was dropped.

Both the rule and its one-second allowance are adjustable where they conflict with measured behaviour ([#164](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/164)) — but only against a measurement, which is the standard the four runs above and the two probes here were held to.
