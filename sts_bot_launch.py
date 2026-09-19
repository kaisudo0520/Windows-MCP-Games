"""給 CommunicationMod 用的啟動腳本：``python -u sts_bot_launch.py [run 的參數]``。

存在的目的是讓模組的 ``command=`` 不需要安裝套件、也不需要 PYTHONPATH，
只要有 Python 和這個 repo 的副本就能跑。所有參數原樣傳給 ``sts-bot run``。
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "src"))

from sts_bot.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["run", *sys.argv[1:]]))
