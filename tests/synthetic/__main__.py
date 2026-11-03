"""合成データを書き出す: python -m tests.synthetic <出力フォルダ> [--seed N]"""

import argparse
from pathlib import Path

from tests.synthetic.generator import DEFAULT_SEED, generate, write_dataset


def main() -> None:
    parser = argparse.ArgumentParser(description="基準ケースの合成データを書き出す")
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()
    files = write_dataset(generate(args.seed), args.out_dir)
    print(f"{len(files)} files written to {args.out_dir}")


if __name__ == "__main__":
    main()
