from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ModelPrice(BaseModel):
    input: float
    cached_input: float = 0.0
    output: float


DEFAULT_PRICES: dict[str, ModelPrice] = {
    "deepseek-v4.1-flash": ModelPrice(input=0.30, cached_input=0.006, output=1.20),
    "deepseek-v4-pro": ModelPrice(input=1.32, cached_input=0.044, output=3.96),
    "deepseek-v4-flash-vision-exp": ModelPrice(input=0.30, cached_input=0.006, output=1.20),
}

ModelRole = Literal["loop", "reason", "vision"]

DEFAULT_MAX_STEPS = 60
DEFAULT_MAX_COST_USD = 5.0


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OPERATOR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    base_url: str = "https://opencode.ai/zen/go/v1"
    api_key: str = ""
    model_loop: str = "deepseek-v4.1-flash"
    model_reason: str = "deepseek-v4-pro"
    model_vision: str = "deepseek-v4-flash-vision-exp"
    llm_mode: Literal["live", "record", "replay"] = "live"
    fixture_dir: Path = Path("tests/fixtures/llm")
    company_dir: Path = Path("company")
    tasks_dir: Path = Path("tasks")
    run_db: Path = Path("runs/operator.db")
    shared_dir: Path = Path("shared")
    mail_db: Path = Path("mocks/state/maildesk.db")
    erp_db: Path = Path("mocks/state/ledgerlite.db")
    max_steps: int = DEFAULT_MAX_STEPS
    max_cost_usd: float = DEFAULT_MAX_COST_USD
    request_timeout_s: float = 120.0
    prices: dict[str, ModelPrice] = Field(default_factory=lambda: dict(DEFAULT_PRICES))

    def model_for(self, role: ModelRole) -> str:
        return {
            "loop": self.model_loop,
            "reason": self.model_reason,
            "vision": self.model_vision,
        }[role]
