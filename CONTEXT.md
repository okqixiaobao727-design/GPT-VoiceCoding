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

**Stop Notice**:
The announcement that a Session has stopped and may need the user, carrying what it stopped on: the question with its options and any recommendation, or the tool awaiting permission with a one-line summary. Spoken into the Live Call and pushed through the Companion Channel — every outlet that is on.

**Delegated Turn**:
Work the system hands to a coding model on the user's behalf during a Live Call, distinct from the call's own speech. Its model is a user-facing setting.
_Avoid_: side request, background query

### Control side

**Bridge Core**:
The one decision-maker: it owns every policy and holds the system's single source of truth. It decides; the modules around it do.
_Avoid_: Bridge Control Center, supervisor, orchestrator, engine (the process, not the role)

**Duty Switch**:
The master on/off switch: off means the system does not speak, does not push, and does not touch the Live Call; events are still recorded. The silence ceiling on a call the system holds still applies — it is the call's own limit, not an act toward the user. Every other switch is effective only while it is on.
_Avoid_: duty mode, pause mode, do-not-disturb

**Voice Switch**:
Whether the system may speak into, open, or otherwise touch the Live Call.
_Avoid_: speech mode, live switch

**Message Switch**:
Whether the system may push messages through the Companion Channel. Independent of the Voice Switch.
_Avoid_: notification switch, push switch

**Feature Switch**:
An independent on/off setting for one capability — a flat boolean under its parent switch.
_Avoid_: mode, profile, sub-mode

**Control Plane**:
Status queries and switch flips, accepted from every surface and never gated by any switch.
_Avoid_: admin commands, management interface

**Control Panel**:
The at-computer surface for seeing the system's current state — the Session roster included — and flipping switches. Runtime state only, not installation settings.
_Avoid_: settings app, preferences window, config tool

**Installation**:
Everything the system places in files the **user** owns so the coding agents can reach it, and takes back byte for byte when asked. Done at first launch and reconciled at every launch after (ADR 0012), never by hand and never by the Control Panel.
_Avoid_: setup, configuration (that is the user's own file, which the system only reads), provisioning

### Reach and sessions

**Companion Channel**:
The pluggable text surface: it pushes the system's messages to the user and accepts their inbound text.
_Avoid_: Telegram (one adapter, not the concept)

**Session**:
One interactive terminal run of Claude Code or Codex. The system sees every Session on the machine, reads what it stopped on, and Relays into it.
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
A Relay of the user's verdict on a Session's pending permission request — one decision for one request, carrying the user's authority.
_Avoid_: auto-approve (the user decides, the system only carries), permission bypass

**Reply Window**:
The state in which a Session will act on the next Relay as its next turn. While it is closed, Relays wait.
_Avoid_: idle state (a Session can be busy yet accepting), input prompt
