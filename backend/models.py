"""Shared schemas. Kept tiny on purpose."""
from typing import Literal, Optional

from pydantic import BaseModel, Field

Severity = Literal["critical", "high", "medium", "low"]
Vital = Literal["usability", "accessibility", "performance", "functional", "integrity"]


class Finding(BaseModel):
    persona: str
    engine: Literal["empathy", "integrity"]
    vital: Vital
    severity: Severity
    title: str
    detail: str = ""
    source: Literal["sensor", "llm", "oracle"] = "sensor"
    url: str = ""
    step: int = 0
    screenshot: str = ""          # path relative to runs/<run_id>/
    persona_quote: str = ""
    suggested_fix: str = ""
    category: str = ""            # integrity findings: fixed set, used to merge differently-worded duplicates


class Persona(BaseModel):
    id: str
    name: str
    engine: Literal["empathy", "integrity"]
    description: str = ""
    intent: str = ""              # why this person came to the site; steers the planner towards a full journey
    device: str = "desktop"
    viewport: dict = Field(default_factory=lambda: {"width": 1280, "height": 800})
    zoom: float = 1.0
    network: Literal["normal", "slow_3g"] = "normal"
    patience_s: float = 5
    rage_click: bool = False
    abandon_at: int = 80
    max_steps: int = 25
    traits: str = ""


class ActorOutput(BaseModel):
    """What the persona-agent returns every step."""
    thought: str = ""
    action: Literal["click", "type", "fill", "select", "scroll", "back", "goto", "wait",
                    "tamper", "repeat", "fill_form", "look", "done", "give_up"] = "wait"
    target: Optional[int] = None
    value: Optional[str] = None
    fields: list[dict] = Field(default_factory=list)   # action=fill: [{"target": id, "value": "..."}, ...]
    confusion: int = 0            # 0-10, how confused the persona is by this screen
    ux_note: str = ""             # concrete issue noticed on this screen, "" if none
    success: Optional[bool] = None  # only meaningful for action=done
    attack: str = ""              # integrity persona: which abuse idea this action belongs to
