# 确保以仓库根目录运行 pytest 时可以导入 app 包
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
