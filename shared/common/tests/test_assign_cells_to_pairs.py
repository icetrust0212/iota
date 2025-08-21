import random
import math
from itertools import combinations
import pytest

from common.utils.partitions import assign_cells_to_pairs


# @pytest.mark.parametrize("n_layer_miners", range(2, 256//5))
@pytest.mark.parametrize("n_layer_miners", range(3, 5))
def test_assign_cells_to_pairs_has_all_pairings(n_layer_miners: int) -> None:
    """Verify that *every* possible pair of submitting miners is present in the
    result of ``assign_cells_to_pairs`` when the number of partitions is derived
    from the total miners in the layer.

    The layer can have more miners than the number that submitted weights. The
    function should still include all unique pairs of the submitting miners at
    least once in the returned mapping.
    """

    layer_miners = [f"miner_{i}" for i in range(n_layer_miners)]
    n_splits = int(max(1, n_layer_miners * (n_layer_miners - 1) / 2))

    # Simulate dropout: for each layer size, run several trials where between
    # 20 % and 80 % of miners *do not* submit. We seed the RNG so these trials
    # are deterministic across test runs.
    rng = random.Random(n_layer_miners)  # deterministic seed per layer size

    n_trials = 5  # number of random dropout scenarios to test per layer size
    for _ in range(n_trials):
        dropout_rate = rng.uniform(0.2, 0.8)
        n_drop = max(1, math.ceil(dropout_rate * n_layer_miners))
        n_submit = max(1, n_layer_miners - n_drop)  # ensure at least one submits

        # print to the terminal the n_submit, the n_drop, and the dropout_rate
        print(
            f"Miners in layer: {n_layer_miners}, n_submit: {n_submit}, n_drop: {n_drop}, dropout_rate: {dropout_rate}"
        )

        submitting_miners = rng.sample(layer_miners, n_submit)

        assignments = assign_cells_to_pairs(miner_hotkeys=submitting_miners, n_partitions=n_splits)

        # Collect the set of *unique* pairs produced by the function.
        returned_pairs = {frozenset(p) for p in assignments.values()}
        print(f"Returned pairs: {returned_pairs}\n")

        if len(submitting_miners) == 1:
            # Special-case: when only one miner submits, the second element in the
            # pair is ``None``.
            assert returned_pairs == {frozenset((submitting_miners[0], None))}
            # When only one miner submits, the function always returns exactly
            # one partition irrespective of the requested ``n_partitions``.
            assert len(assignments) == n_splits
            continue

        # The function should include every possible combination of the
        # submitting miners *at least once*.
        expected_pairs = {frozenset(p) for p in combinations(submitting_miners, 2)}
        # Because the implementation may add extra (possibly duplicate) pairs to
        # reach ``n_partitions``, we only check that each expected pair is
        # present, not that the sets are equal.
        assert expected_pairs.issubset(returned_pairs)

        # Finally, ensure the correct number of partitions were generated.
        assert len(assignments) == n_splits


def test_duplicate_assignments() -> None:
    """Verify that *every* possible pair of submitting miners is present in the
    result of ``assign_cells_to_pairs`` when the number of partitions is derived
    from the total miners in the layer.
    """
    miner_counts = [1, 2] + random.sample(range(1, 200), 20)
    for miner_count in miner_counts:
        submitted_count = random.randint(1, miner_count)
        n_splits = int(max(1, miner_count * (miner_count - 1) / 2))

        miner_hotkeys = [f"miner_{i}" for i in range(submitted_count)]

        assignments = assign_cells_to_pairs(
            miner_hotkeys=miner_hotkeys,
            n_partitions=n_splits,
        )
        assert len(assignments) == n_splits, "The number of partitions should be equal to the number of splits"
        for partition in assignments.values():
            assert partition[0] != partition[1], "The two miners in the partition should be different"

        # Count the number of times each miner appears in the partitions
        miner_counts = {miner: 0 for miner in miner_hotkeys}
        for partition in assignments.values():
            for miner in partition:
                # None occurs if there is only a single miner with submitted weights
                if miner is None:
                    continue
                miner_counts[miner] += 1

        # we expect that the number of partitions assigned to each miner is roughly equal, within some margin.
        counts = list(miner_counts.values())
        if abs(min(counts) - max(counts)) / max(counts) > 0.2:
            if max(counts) - min(counts) > 4:
                raise Exception(f"Miner counts too different (min: {min(counts)}, max: {max(counts)}): {miner_counts}")
