from itertools import islice
import numpy as np
from ptls.data_load.iterable_processing_dataset import IterableProcessingDataset


class IterableShuffle(IterableProcessingDataset):
    """
    Shuffle records in the buffer and yield them in random order. Buffer is filled with records
    from the source iterator. When buffer is empty, the iterator is exhausted. Buffer is refilled
    with records from the source iterator and the process is repeated.

    Args:
        buffer_size: buffer size in records

    """
    def __init__(self, buffer_size: int):
        super().__init__()

        assert buffer_size > 1  # we will split buffer into two parts
        self._buffer_size = buffer_size

    def __iter__(self):
        source = iter(self._src)
        buffer = np.array([])
        while True:
            new_buffer_size = self._buffer_size - len(buffer)
            new_buffer_list = list(islice(source, new_buffer_size))
            new_buffer = np.empty(len(new_buffer_list), dtype=object)
            new_buffer[:] = new_buffer_list
            buffer = np.concatenate([buffer, new_buffer])
            if len(buffer) == 0:
                break

            window_size = min(len(buffer), self._buffer_size // 2)
            ix_for_choice = np.random.choice(len(buffer), window_size, replace=False)

            for idx in ix_for_choice:
                yield buffer[idx]

            mask_selected = np.zeros(len(buffer), dtype=bool)
            mask_selected[ix_for_choice] = True
            buffer = buffer[~mask_selected]
