"""Configured projects and their spoken lookup live inside Bridge Core.

This is an internal deep module, not a seam.  It owns no adapter and no state
copy; the composition root hands Bridge Core the immutable configuration it
read, and callers see project resolution only through the existing launch verb.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from pathlib import Path

from gpt_voicecoding.core.errors import AmbiguousProjectError, UnknownProjectError


@dataclass(frozen=True, slots=True)
class Project:
    """One canonical project and the names speech may use to refer to it."""

    name: str
    workspace: Path
    spoken_aliases: tuple[str, ...] = ()


def _spoken_name(value: str) -> str:
    """A loose deterministic key that ignores typography but never guesses spelling."""
    folded = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in folded if character.isalnum())


@dataclass(frozen=True, slots=True)
class ProjectCatalogue:
    """Resolve one spoken reference to exactly one configured project."""

    projects: tuple[Project, ...]

    def resolve(self, reference: str) -> Project:
        wanted = _spoken_name(reference)
        if not wanted:
            raise UnknownProjectError(
                reference, tuple(project.name for project in self.projects)
            )
        matches = tuple(
            project
            for project in self.projects
            if wanted
            in {_spoken_name(name) for name in (project.name, *project.spoken_aliases)}
        )
        if not matches:
            raise UnknownProjectError(reference, tuple(project.name for project in self.projects))
        if len(matches) != 1:
            raise AmbiguousProjectError(reference, tuple(project.name for project in matches))
        return matches[0]
