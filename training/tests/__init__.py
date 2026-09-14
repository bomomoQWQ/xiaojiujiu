"""测试包。

添加 ``__init__.py`` 让 ``tests`` 成为真正的包，测试模块之间可以
``from tests.conftest import FakeDeepSeekServer`` 复用假服务端，
而不依赖 ``sys.path`` 的偶然状态。
"""

from __future__ import annotations
