"""Pytest 配置文件"""

import sys
import types
from pathlib import Path

import pytest

project_root = Path(__file__).resolve().parent.parent

# 以固定包名注册插件根目录，模拟 AstrBot 运行时的
# data.plugins.<插件目录名> 包前缀：main.py / iris_memory 的相对导入
# 在测试中可用，且不依赖仓库目录名、不污染 sys.path。
if "astrbot_plugin_iris_memory" not in sys.modules:
    _pkg = types.ModuleType("astrbot_plugin_iris_memory")
    _pkg.__path__ = [str(project_root)]
    _pkg.__package__ = "astrbot_plugin_iris_memory"
    sys.modules["astrbot_plugin_iris_memory"] = _pkg


@pytest.fixture
def tmp_data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


# ---------------------------------------------------------------------------
# 角色身份（iris_memory.identity）：测试统一使用虚构的名字与虚构的参照角色
# ---------------------------------------------------------------------------
TEST_BOT_NAMES = "小白,xiaobai,しろ,シロ"
TEST_OWNER_NAMES = "aming,小明"
# 虚构作品《星海物语》：用来测"参照角色"规则的加载与拦截（规则 id 与通用规则同一编号体系）
TEST_REFERENCE = {
    "work_mention": r"星海物语|星海物語|星海门|星海門|star\s*sea",
    "copy_hard": [
        ["COPY-H01", r"啾+比+啾+|啾啾比", False],
        ["COPY-H06", r"星君|星くん|hoshi-?kun", True],
    ],
    "copy_soft": [
        ["COPY-S01", r"星光怀表|starlight\s*watch", True],
        ["COPY-S07", r"星轨|星軌|star\s*track", True],
        ["COPY-S14", r"海老师|鲸鱼先生", False],
    ],
    "reviewer_note": "尤其是《星海物语》里角色的招牌问候。",
}


@pytest.fixture(autouse=True)
def _test_identity():
    from astrbot_plugin_iris_memory.iris_memory import identity

    identity.reset()
    identity.configure(bot_names=TEST_BOT_NAMES, owner_names=TEST_OWNER_NAMES, reference=TEST_REFERENCE)
    yield
    identity.reset()
