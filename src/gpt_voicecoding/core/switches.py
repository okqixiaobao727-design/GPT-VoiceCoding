"""Switch state — one of Bridge Core's three pieces of truth.

The hierarchy is `CONTEXT.md`'s, not this module's invention: the Duty Switch is
master, the Voice and Message Switches sit beneath it, and Feature Switches are
flat booleans under a parent. Every switch has exactly two states, so every
switch here is a `bool` and nothing else.

Two words are deliberately different:

- **set** — the flag as the user last flipped it. Flipping Duty off never
  rewrites what is underneath it, so flipping Duty back on restores the state the
  user chose rather than a default.
- **effective** — the flag *and* every ancestor's, up to and including Duty. This
  is the only question business behaviour may ask.

Which Feature Switches exist is configuration, passed in at construction. This
module knows the three named switches because `CONTEXT.md` names them; it knows
no feature by name.

ADR 0002 is *not* implemented here and must not be: the rule that the control
plane is never gated is a policy about which callers consult this state, and it
lives with the policy pipelines.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from gpt_voicecoding.core.errors import UnknownSwitchError


class SwitchName(StrEnum):
    """The three switches `CONTEXT.md` names. Feature Switches are not members."""

    DUTY = "duty"
    VOICE = "voice"
    MESSAGE = "message"


#: Every named switch's parent. Duty has none — it is the master.
_NAMED_PARENTS: dict[SwitchName, SwitchName | None] = {
    SwitchName.DUTY: None,
    SwitchName.VOICE: SwitchName.DUTY,
    SwitchName.MESSAGE: SwitchName.DUTY,
}


@dataclass(frozen=True, slots=True)
class FeatureSwitch:
    """One capability's on/off setting, declared by configuration.

    ``parent`` is the switch it hangs from — usually Voice or Message. There are
    no combined "modes" and no nesting below a feature.
    """

    name: str
    parent: SwitchName
    default: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.default, bool):
            raise TypeError(f"a switch has exactly two states; {self.default!r} is neither")


@dataclass(frozen=True, slots=True)
class SwitchSnapshot:
    """Every switch's set state, in a form that survives a restart.

    Ordered by name so two snapshots of equal state compare equal.
    """

    states: tuple[tuple[str, bool], ...]

    def as_mapping(self) -> dict[str, bool]:
        return dict(self.states)

    @classmethod
    def of(cls, states: dict[str, bool]) -> SwitchSnapshot:
        return cls(tuple(sorted(states.items())))


class Switchboard:
    """The switch state itself. Holds flags; decides nothing."""

    def __init__(self, features: Iterable[FeatureSwitch] = ()) -> None:
        parents: dict[str, str | None] = {
            str(name): (str(parent) if parent is not None else None)
            for name, parent in _NAMED_PARENTS.items()
        }
        states: dict[str, bool] = dict.fromkeys(parents, False)

        for feature in features:
            if feature.name in parents:
                raise ValueError(
                    f"feature switch {feature.name!r} shadows an existing switch of that name"
                )
            parents[feature.name] = str(feature.parent)
            states[feature.name] = feature.default

        self._parents = parents
        self._states = states

    def names(self) -> tuple[str, ...]:
        """Every switch this board holds, named switches first."""
        return tuple(self._states)

    def is_set(self, name: str) -> bool:
        """The flag as last flipped, ignoring everything above it."""
        return self._states[self._known(name)]

    def is_effective(self, name: str) -> bool:
        """The flag *and* every ancestor's — the only question behaviour may ask."""
        current: str | None = self._known(name)
        while current is not None:
            if not self._states[current]:
                return False
            current = self._parents[current]
        return True

    def flip(self, name: str, on: bool) -> bool:
        """Set a switch, returning the state it held before.

        Refuses anything that is not a `bool`. A string `"false"` is truthy, so
        a switch read optimistically would look off and behave on — and the one
        that matters most is the master.
        """
        if not isinstance(on, bool):
            raise TypeError(f"a switch has exactly two states; {on!r} is neither")
        key = self._known(name)
        previous = self._states[key]
        self._states[key] = on
        return previous

    def snapshot(self) -> SwitchSnapshot:
        return SwitchSnapshot.of(self._states)

    def restore(self, snapshot: SwitchSnapshot) -> None:
        """Adopt a persisted snapshot.

        A snapshot naming a switch this board does not have fails closed: a
        Feature Switch dropped from configuration must not resurrect from disk.
        Switches the snapshot does not mention keep their configured default.
        """
        for name, state in snapshot.states:
            self._known(name)
            if not isinstance(state, bool):
                raise TypeError(f"a switch has exactly two states; {state!r} is neither")
        for name, state in snapshot.states:
            self._states[name] = state

    def _known(self, name: str) -> str:
        key = str(name)
        if key not in self._states:
            raise UnknownSwitchError(key)
        return key
