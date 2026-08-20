# 4. The engine owns its log, so rotation can rename rather than truncate

Date: 2026-08-18

Status: Accepted

Carried over from: [ADR 0004 of the reference implementation](https://github.com/okqixiaobao727-design/GPT-VoiceCoding-legacy/blob/main/docs/adr/0004-bounded-log-files.md),
which holds the full measurement and the adapter-log limitation. The principle is
carried; its reference-implementation mechanics are not.

## Context

The reference implementation's log had no rotation, no truncation and no size cap.
It reached 68,042,451 bytes in 49.5 hours — ~1.37 MB/hour, ~1 GB/month — and 98.1%
of those bytes were a single `libmalloc` line repeated 681,929 times, emitted by
every spawned subprocess because a `MallocStackLogging` variable had been inherited
from whichever shell started the daemon. The 105 lines that explained a real outage
were buried under it.

The cause of the volume was also the reason rotation was hard: the daemon did not
own its log. A shell redirect had handed it a descriptor and no way to be told the
file had moved, so rename-based rotation would have left the biggest writer
appending to the renamed file — rotation that looks like it worked while fixing
nothing.

Copy-truncate keeps the inode and was tried. It fails on correctness: a copy
followed by a truncate is two operations, and a line appended between them is
gone.

## Decision

**The engine owns its log.** It opens the configured log file itself and `dup2`s it
onto its own stdout and stderr; nothing that starts the engine redirects its output
anywhere. Whatever descriptor the launching shell supplies is replaced at adoption,
which happens before the engine object exists — so in practice it carries only an
interpreter-level failure, and never outlives adoption.

**Rotation is rename-and-reopen.** The live file is renamed, and the owner reopens
the path and re-points stdout and stderr at the new file. Every byte written before
the rename is in the rotated generation and every byte after it is in the new live
file. There is no window in which a write can be dropped.

**The cap is real and binds every generation**, not only the one a rotation just
created — a file can be over the cap without this rotation having put it there.
Rotation keeps the **newest** bytes of what it rotates and discards the rest: the
tail is the part that explains what just happened.

**Noise is stripped at the environment, not at each spawn site.** Variables whose
names match a configured prefix are removed once, by every process that spawns
others.

**Three of the four values have no fallback in code**: max bytes, retained
generations and stripped prefixes are decisions this outage measured, and a
default compiled in beside them would quietly reinstate a number the measurement
proved matters. The log *path* is a location rather than a decision, so it
defaults beside the state file by the same rule the state file and the socket
follow — see the note in `config.py`. Amended during the port (issue #4), because
the sentence this replaces lumped the path in with the three and the code would
otherwise have contradicted it silently.

## Consequences

Any log the engine cannot tell to reopen — one held open for its whole life by a
third-party child process — cannot use rename-and-reopen and falls back to
truncate-in-place, accepting that rollover window for that log only. It must never
be `bridge.logFile`. The reference implementation hit exactly this with the Codex
app-server logs; see its ADR for the reasoning.

**A child that is already running when a rotation happens cannot be told to
reopen either.** Its inherited descriptor keeps referring to the file that was
renamed, so its output rides the generation chain from that point on — carried
along by later rotations and dropped by retention like any other old bytes,
rather than following the engine into the new live file. Two things follow, and
both are load bearing: a rotated generation is trimmed **on its own inode** and
never by replacing the file, because a replacement would leave that child writing
into an unlinked inode for the rest of its life; and the only way to remove the
limitation rather than bound it is for the launcher to give its children a pipe
the engine reads, instead of the log descriptor itself. Recorded during the port
(issue #4), where both were measured; the pipe is left to the launcher's ticket.

Output produced before adoption (argument parsing, configuration loading) has
nowhere to go and is discarded. An engine that dies that early never answers a
status query, so the failure is still surfaced — by silence on the socket rather
than by a log line.
