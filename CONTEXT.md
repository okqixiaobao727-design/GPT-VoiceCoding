# GPT-VoiceCoding

Voice-controlling terminal coding agents through a realtime voice call: the system speaks agent progress to the user and carries the user's spoken instructions back to the agents.

## Language

### Voice side

**Live Call**:
The system-owned realtime voice call — the system's one and only voice surface.
_Avoid_: Live thread, voice chat, call (unqualified)

**Live Toggle**:
The single action that starts a Live Call when none is up, or ends the current one. One toggle, not separate start/stop commands.
_Avoid_: toggle phrase

**Stop Notice**:
The spoken announcement, delivered into a Live Call, that a coding-agent session has stopped and may need the user.

**Delegated Turn**:
Work the system hands to a coding model on the user's behalf during a Live Call, distinct from the call's own speech. Its model is a user-facing setting.
_Avoid_: side request, background query

### Control side

**Bridge Core**:
The one decision-maker: it owns every policy and holds the system's single source of truth. It decides; the modules around it do.
_Avoid_: Bridge Control Center (collides with Control Plane / Control Panel), supervisor, orchestrator, engine (that is the process, not the role)

**Duty Switch**:
The master on/off switch for the whole voice coordination: off means the system does not speak, does not push, and does not touch the Live Call; events are still recorded. Every other switch is effective only while it is on. Exactly two states.
_Avoid_: duty mode, pause mode, do-not-disturb

**Voice Switch**:
The switch for everything spoken: whether the system may speak into, open, or otherwise touch the Live Call.
_Avoid_: speech mode, live switch

**Message Switch**:
The switch for text reach: whether the system may push messages through the Companion Channel. Independent of the Voice Switch — messages-only operation is a supported state.
_Avoid_: notification switch, push switch

**Feature Switch**:
An independent on/off setting for one capability. Feature Switches are flat booleans under their parent switch; there are no combined "modes".
_Avoid_: mode, profile, sub-mode

**Control Plane**:
Status queries and switch flips, accepted from every surface and never gated by any switch.
_Avoid_: admin commands, management interface

**Control Panel**:
The at-computer surface for seeing the system's current state and flipping switches directly. Runtime state only — not an editor for installation settings.
_Avoid_: settings app, preferences window, config tool

### Reach and sessions

**Companion Channel**:
The pluggable channel that reaches the user when no Live Call is up, and accepts their inbound text.
_Avoid_: Telegram (one adapter, not the concept)

**Session**:
One terminal coding-agent run (Claude Code or Codex) that the system launches, watches, and Relays into.
_Avoid_: task, job, window

**Session Launcher**:
The capability that brings a Session into existence in a workspace.
_Avoid_: terminal control, tmux (one optional way to launch, not the capability)

**Relay**:
Carrying words *into* a Session — the agent-ward direction.
_Avoid_: injection (a mechanism, not the capability), push, channel (reserved for the Companion Channel)

**Answer Relay**:
A Relay of the user's own words — their spoken instructions and their answers to a Session's questions — carrying the user's authority.
_Avoid_: MCP Channel (one adapter, not the capability)

**Notice Relay**:
A Relay of words the system itself originates; it neither needs nor claims the user's authority.
_Avoid_: system message, notification (that word belongs to the user-ward direction)

**Approval Relay**:
A Relay of the user's verdict on a Session's pending permission request — one decision for one request, carrying the user's authority.
_Avoid_: auto-approve (the user decides, the system only carries), permission bypass

**Reply Window**:
The state in which a Session can accept an inbound Relay as a user turn — open when the Session is awaiting input. While it is closed, Relays wait.
_Avoid_: idle state (a Session can be busy yet accepting), input prompt
