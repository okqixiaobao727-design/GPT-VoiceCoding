# GPT-VoiceCoding

A bridge over every coding-agent Session on the user's Mac: it tells the user what each Session is doing and what it stopped on, carries the user's words into it, and calls the user when a Session needs a decision.

## Language

### Voice side

**Live Call**:
The system-owned realtime voice call — the system's one and only voice surface.
_Avoid_: Live thread, voice chat, call (unqualified)

**Live Toggle**:
The single action that starts a Live Call when none is up, or ends the current one.
_Avoid_: toggle phrase

**Call Keeper**:
The part of Bridge Core that keeps the Live Call's time: when a call is dialled, when it ends, and when the system may ring or speak into it. It knows nothing of what is said — it asks for a fresh reading at the moment it decides to sound.
_Avoid_: interlock (the mechanism it grew from), call manager, scheduler

**Silence Ceiling**:
The automatic hang-up: a call ends after a configured stretch in which neither the user spoke nor the call's own voice sounded. Time spent while the voice is speaking does not count. Governed by the Auto Hang-up Switch.
_Avoid_: idle timeout, inactivity timer

**Session Brief**:
What the system knows about one Session, structured for telling the user: its name, its agent, its state (waiting for a decision, requesting permission, finished, running, or unreadable), its newest message, and the decision it is waiting on — the question with its options and any recommendation, or the tool awaiting permission with a one-line summary — and, when the user's last reply to it never arrived, that it did not and why. The summary the user hears and the detail they may ask for are one and the same facts.
_Avoid_: 单项目简报, notice (unqualified), stop detail

**Roster Brief**:
The count of Sessions in each state, with one header row per live Session. Spoken when several Sessions need the user, or on request.
_Avoid_: 多项目简报, overview, summary

**Focus Session**:
The one Session the user last replied to — by Answer Relay or Approval Relay. Its news is spoken first; another Session's news only rings. Cleared when it ends; never set by merely asking about a Session.
_Avoid_: current session, active session, last session

**Detail**:
The full form of a Session Brief's facts, given when the user asks: the newest message whole (content detail) and the decision point whole — question, every option, the recommendation (decision detail).
_Avoid_: stop detail, expansion

**History**:
What one Session said and was told before its newest message, read on request in pages of a configured size (default five entries, both sides counted, newest first). A page names each entry's place in the Session's record so the next request can ask for the entries before it; an entry too large to carry is named as omitted, never dropped without a word. The page size is a count; the wire's byte ceiling stays a ceiling.
_Avoid_: progress (the retired verb), transcript, log, tail

**Stop Notice**:
A Session Brief published as text — what the Companion Channel receives when a Session stops and may need the user. The Live Call does not receive text to read out; it receives the Session Brief itself and speaks from it.
_Avoid_: announcement (the act, not the thing)

**Cool-down**:
The minimum interval between two sounds the system makes toward the user on the voice side: after any end of a call — hung up, dropped, or a dial that failed — it does not dial again; during a call it does not ring or speak unbidden again. A Session event inside it only marks that a call, or a word, is owed; when it ends, the system reads the Sessions afresh and dials, speaks, or stays quiet on what it finds — never on replayed events. The user's own Live Toggle is not subject to it.
_Avoid_: debounce, back-off, grace period, rate limit

**Voice**:
The Live Call's speaking half — the model the user hears and talks to. It has no tools: it composes speech from what the engine hands it, and hands anything that reads as a job to the Call Agent. It is addressed in plain prose, never in code-like text.
_Avoid_: voice model, realtime model, assistant (unqualified), voice thread

**Call Agent**:
The Live Call's acting half — the coding model behind the Voice and the only one on the call with tools. It runs the control-plane verbs the Voice hands it. Not a Delegated Turn, which is work the system hands out on purpose.
_Avoid_: backing Codex model, the agent behind the call, delegate

**Delegated Turn**:
Work the system hands to a coding model on the user's behalf during a Live Call, distinct from the call's own speech. Its model is a user-facing setting.
_Avoid_: side request, background query

### Control side

**Bridge Core**:
The one decision-maker: it owns every policy and holds the system's single source of truth. It decides; the modules around it do.
_Avoid_: Bridge Control Center, supervisor, orchestrator, engine (the process, not the role)

**Duty Switch**:
The master on/off switch: off means the system does not speak, does not ring, does not push, and does not touch the Live Call; events are still recorded. The Silence Ceiling still applies — it is the call's own limit, not an act toward the user. The Voice and Message Switches and every Feature Switch are effective only while it is on; the Auto Hang-up Switch stands beside it, not under it.
_Avoid_: duty mode, pause mode, do-not-disturb

**Voice Switch**:
Whether the system may speak into, open, or otherwise touch the Live Call.
_Avoid_: speech mode, live switch

**Message Switch**:
Whether the system may push messages through the Companion Channel. Independent of the Voice Switch.
_Avoid_: notification switch, push switch

**Auto Hang-up Switch**:
Whether the Silence Ceiling ends a call. On by default. It stands beside the Duty Switch, under nothing: the ceiling is the call's own limit, not an act toward the user, so it holds with Duty off and on calls the user opened.
_Avoid_: auto-end flag, feature switch (it has no parent)

**Feature Switch**:
An independent on/off setting for one capability — a flat boolean under its parent switch.
_Avoid_: mode, profile, sub-mode

**Control Plane**:
Status queries and switch flips, accepted from every surface and never gated by any switch.
_Avoid_: admin commands, management interface

**Control Panel**:
The at-computer surface for seeing the system's current state — the Session roster included — and flipping switches, plus the shell-owned Companion Channel credential. Runtime state and that one write-only credential, not installation settings.
_Avoid_: settings app, preferences window, config tool

**Installation**:
Everything the system places in files the **user** owns so the coding agents can reach it, and takes back byte for byte when asked. Done at first launch and reconciled at every launch after (ADR 0012), never by hand and never by the Control Panel.
_Avoid_: setup, configuration (that is the user's own file, which the system only reads), provisioning

### Reach and sessions

**Companion Channel**:
The pluggable text surface: it pushes the system's messages to the user and accepts their inbound text.
_Avoid_: Telegram (one adapter, not the concept)

**Session**:
One interactive terminal run of Claude Code or Codex. The system sees every Session on the machine, reads what it stopped on, and Relays into it. It sees one by recognising it from what the machine already shows — never by wrapping or instrumenting it — so a Session it cannot recognise is under-reported and said to be, never invented (ADR 0020).
_Avoid_: task, job, window, launched Session (the system launches nothing)

**Child Process**:
A process a Session spawns — a subagent, a review crew. It appears in the roster under its Session and nothing more: no Relay, no Stop Notice, no name.
_Avoid_: child Session, subagent (the agent's mechanism word), crew

**Session Name**:
What the user and the system call one Session: `<project> · <task>`, where the project is the
Git repository the Session is working in — its own directory when it is in none — and the task
is the agent's own name for the Session. Composed by the lane that first saw the Session and
changed only when its official source renames it — the user says it back to address the
Session, so there is one name at a time and nothing else may move it.
_Avoid_: label, title

**Relay**:
Carrying words *into* a Session — the agent-ward direction.
_Avoid_: injection (a mechanism, not the capability), push, channel (reserved for the Companion Channel)

**Answer Relay**:
A Relay of the user's own words — their instructions and their answers to a Session's questions.
_Avoid_: MCP Channel (one adapter, not the capability)

**Approval Relay**:
A Relay of the user's verdict on a Session's pending permission request — one decision for one request, carrying the user's authority. It carries and nothing more: the request is briefed as the Session's PERMISSION state like any other, the hook's own life bounds how long a verdict can land, and the outcome is the receipt the verb returns.
_Avoid_: auto-approve (the user decides, the system only carries), permission bypass, approval budget (the engine keeps none), closing notice (retired)

**Reply Window**:
The state in which a Session will act on the next Relay as its next turn. While it is closed, Relays wait.
_Avoid_: idle state (a Session can be busy yet accepting), input prompt
