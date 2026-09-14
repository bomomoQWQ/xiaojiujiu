"""训练工程的异常类型。"""

from __future__ import annotations


class TrainingError(Exception):
    """本工程所有自定义异常的基类。"""


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
