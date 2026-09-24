"""训练工程的异常类型。"""

from __future__ import annotations


class TrainingError(Exception):
    """本工程所有自定义异常的基类。"""


class ToolUnavailable(TrainingError):
    """外部工具不存在（llama.cpp 的转换脚本 / 量化程序等）。

    单独成一个类型，是为了让 ``--dry-run`` 能区分两种情况：

      * **工具没装**：dry-run 下可以继续，用占位路径展示将要执行的命令；
      * **用户显式给了错误路径**（如 ``--convert-script`` 指向不存在的文件）：
        这是输入错误，必须直接报错，不能因为 dry-run 就假装成功。
    """


class JsonExtractionError(TrainingError):
    """从模型回复中抽取 JSON 失败。"""


class SchemaViolation(TrainingError):
    """数据不符合契约（JSON Schema 或附加不变量）。"""


class BudgetExceeded(TrainingError):
    """预算守卫触发，生成器应停止并保留断点。"""


class GeneratorError(TrainingError):
    """数据生成器运行期错误。"""


class ConfigError(TrainingError):
    """配置错误。"""


class MissingCredentialError(TrainingError):
    """缺少必要的凭据（例如 DEEPSEEK_API_KEY）。"""

    def __init__(self, env_name: str) -> None:
        super().__init__(
            f"环境变量 {env_name} 未设置。本工程绝不把 API key 写入任何文件；"
            f"请在 shell 中设置：\n"
            f'  PowerShell:  $env:{env_name} = "sk-..."   (仅当前会话)\n'
            f"  bash:        export {env_name}=sk-..."
        )
        self.env_name = env_name
