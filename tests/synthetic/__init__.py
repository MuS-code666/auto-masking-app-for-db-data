"""合成データ生成器。実データの代わりに試験で使う。"""

from tests.synthetic.generator import DEFAULT_SEED, Dataset, Table, generate, write_dataset

__all__ = ["DEFAULT_SEED", "Dataset", "Table", "generate", "write_dataset"]
