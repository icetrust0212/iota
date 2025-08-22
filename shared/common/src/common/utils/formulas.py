import math
from common import settings as common_settings


def calculate_n_partitions(n_miners: int) -> int:
    return int(max(1, n_miners * (n_miners - 1) / 2))


def calculate_num_parts(data: bytes) -> int:
    return int(math.ceil(len(data) / common_settings.MAX_PART_SIZE))
