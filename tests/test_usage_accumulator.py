from engine.types import APIUsage
from engine.usage import UsageAccumulator


def test_effective_context_counts_the_last_calls_output_not_the_cumulative_output() -> None:
    usage = UsageAccumulator()
    usage.update(APIUsage(input_tokens=100_000, output_tokens=5_000))
    usage.update(APIUsage(input_tokens=110_000, output_tokens=4_000))
    assert usage.output == 9_000
    assert usage.effective_context_tokens() == 114_000

    # After a compaction the context is small again, however much the session
    # has produced in total.
    usage.output = 8_000_000
    usage.last_input = 27_000
    usage.last_output = 0
    usage.last_cache_read = 0
    usage.last_cache_write = 0
    assert usage.effective_context_tokens() == 27_000
