"""理解痞老板 Runtime 的 2B 模型训练工程。

模块划分
--------
``contracts``   任务契约（JSON Schema + 提示词 + 字段分类）
``config``      配置模型与加载
``data``        DeepSeek 数据生成、种子场景、IO
``validators``  离线校验（schema + 不变量 + 文本约束）
``sft``         聊天 SFT 数据构建（completion-only 掩码）
``eval``        评测（合法率 / 准确率 / 误差 / 不变量 / 文本约束）
``training``    LoRA / QLoRA 训练、adapter 合并、GGUF 导出
``benchmark``   CPU 推理基准
``inference``   本地推理后端（评测与 benchmark 共用）
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
