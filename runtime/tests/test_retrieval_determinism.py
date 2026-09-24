"""Retrieval ranking must be reproducible: the same cue recalls the same memories.

The exploration term used to be ``uniform(0, random_epsilon)`` drawn per memory on every
call - and the *selection* path drew it from an unseeded ``random.Random()``, because
``context`` calls ``retrieve`` without the Runtime's seeded rng. A cue is a Chinese
sentence and the lexical term is CJK bigram overlap, so several memories routinely land
inside the 0.03 band and the ranking became a coin flip. Measured: a two-and-a-half-month
end-to-end simulation failed its cued-recall check in about one run in nine, with the
faded memory staying ``low_activation`` while eight others were recalled in the same step.

The offset is now a pure function of the memory id, so it still spreads memories out and
a memory that trails by less than the band is not permanently ordered by its identifier,
while the same cue recalls the same memories in every run and every process.
"""

from __future__ import annotations

from companion_runtime.memory import stable_exploration_offset


def test_the_offset_is_a_pure_function_of_the_memory() -> None:
    """Two calls, one answer - this is the property the coin flip broke.

    A mutation that restores a fresh random draw fails here on the first call.
    """
    first = stable_exploration_offset("mem_interview", span=0.03)
    second = stable_exploration_offset("mem_interview", span=0.03)
    assert first == second
    assert 0.0 <= first < 0.03


def test_different_memories_get_different_offsets() -> None:
    """The term still explores: otherwise every memory would collapse onto one score."""
    offsets = {stable_exploration_offset(f"mem_{index}", span=0.03) for index in range(50)}
    assert len(offsets) > 40, "the offsets must spread over the band, not pile up"
    assert all(0.0 <= value < 0.03 for value in offsets)


def test_a_zero_span_means_no_offset_at_all() -> None:
    """The knob still controls the band, including switching it off."""
    assert stable_exploration_offset("mem_interview", span=0.0) == 0.0
