"""The Session Launcher seam — bringing a Session into existence in a workspace.

Verbs Bridge Core calls: launch a Session into a workspace; report the launch
outcome.

Launching and conversing are orthogonal: this seam only creates Sessions, and the
Agent seam talks to them. The Session *registry* is Bridge Core state, not a
module — there is deliberately no Session module (ADR 0001).

Adapters: tmux (optional) and a direct child process.
"""
