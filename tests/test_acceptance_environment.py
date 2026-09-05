"""What the verdict records about the machine a run was taken on (#230).

Why it records anything is `support.HostPressure`'s own docstring and is not
restated here. What is graded here is the *parsing*, against real `vm_stat` and
`vm.swapusage` output captured from this machine — because a number nobody can
read back is the one failure these three have, and it would show up months
later as a `null` on the one run that needed them.

Reading the live host is deliberately not graded: a test that asserted a number
the machine chooses would be grading the machine.
"""

from __future__ import annotations

import subprocess

import pytest
import support

#: `vm_stat` on macOS 15, trimmed to the lines the sum reads plus two it must
#: not. The trailing period on every count is real, and is what a naive `int()`
#: chokes on.
VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                     6282.
Pages active:                                 195105.
Pages inactive:                               193588.
Pages speculative:                              1192.
Pages throttled:                                   0.
Pages wired down:                             225594.
Pages purgeable:                                   4.
"""

#: `sysctl -n vm.swapusage`, verbatim.
SWAPUSAGE = "total = 2048.00M  used = 1097.94M  free = 950.06M  (encrypted)\n"


class TestReadingTheHostsMemory:
    def test_free_memory_is_the_pages_a_new_allocation_could_have(self) -> None:
        """Free plus inactive plus speculative, at the page size `vm_stat` names.

        Not "Pages free" alone: on a warm macOS that number is near zero on a
        machine with gigabytes available, so a run would record something that
        looks like pressure on every host and distinguishes nothing.
        """
        assert support.free_memory_from(VM_STAT) == (6282 + 193588 + 1192) * 16384

    def test_the_page_size_is_read_and_never_assumed(self) -> None:
        """Apple silicon pages at 16K and Intel at 4K, and the run may be on either."""
        intel = VM_STAT.replace("page size of 16384 bytes", "page size of 4096 bytes")

        assert support.free_memory_from(intel) == (6282 + 193588 + 1192) * 4096

    def test_output_that_is_not_vm_stat_is_no_answer_rather_than_a_wrong_one(self) -> None:
        """A recorded number nobody can trust is worse than a recorded `null`."""
        assert support.free_memory_from("") is None
        assert support.free_memory_from("Pages free: 10.\n") is None
        assert support.free_memory_from(VM_STAT.replace("Pages inactive", "Pages idle")) is None

    def test_swap_used_is_the_middle_figure_in_megabytes(self) -> None:
        assert support.swap_used_from(SWAPUSAGE) == pytest.approx(1097.94 * 1024 * 1024)

    def test_a_machine_with_no_swap_in_use_reads_as_zero_not_as_absent(self) -> None:
        """Zero is the fact that makes a clean run's row worth comparing against."""
        none = "total = 0.00M  used = 0.00M  free = 0.00M\n"

        assert support.swap_used_from(none) == 0

    def test_swap_output_it_cannot_read_is_no_answer(self) -> None:
        assert support.swap_used_from("") is None
        assert support.swap_used_from("used = lots") is None


class TestWhatTheVerdictCarries:
    def test_the_three_numbers_reach_the_environment_block(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The block a reader of a stalled run opens first, with the host in it."""
        monkeypatch.setattr(
            support, "_read", lambda command, **_: {"vm_stat": VM_STAT}.get(command[0])
        )
        monkeypatch.setattr(support.os, "getloadavg", lambda: (1.5, 2.0, 2.5))

        facts = support.HostPressure.read().as_facts()

        assert facts["host_free_memory_bytes"] == (6282 + 193588 + 1192) * 16384
        assert facts["host_swap_used_bytes"] is None
        assert facts["host_load_average"] == [1.5, 2.0, 2.5]

    def test_a_host_that_will_not_answer_is_recorded_as_unknown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """These are notes on the side, and a run must not fail for want of them.

        The acceptance is graded on the product; a missing `vm_stat` is not a
        finding about the product, and raising here would turn one into a
        refusal.
        """
        monkeypatch.setattr(support, "_read", lambda _command, **_: None)
        monkeypatch.setattr(support.os, "getloadavg", lambda: (_ for _ in ()).throw(OSError()))

        facts = support.HostPressure.read().as_facts()

        assert facts == {
            "host_free_memory_bytes": None,
            "host_swap_used_bytes": None,
            "host_load_average": None,
        }

    def test_a_tool_that_is_not_there_is_not_an_exception(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The host reads must survive a machine without the tool.

        They are notes on the side of a verdict, so `None` is the answer and a
        raise would refuse a run over a missing `vm_stat`. The gen-1 shell-wrapper
        probe deliberately does *not* come through here — see `environment_facts`.
        """

        def missing(*_args: object, **_kwargs: object) -> None:
            raise FileNotFoundError("no such tool")

        monkeypatch.setattr(subprocess, "run", missing)

        assert support._read(["vm_stat"], timeout_seconds=1.0) is None
