from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


Side = Literal["me", "other", "system"]
ContextSource = Literal["vision_api", "ocr_local", "manual"]


class ChatMessage(BaseModel):
    side: Side
    text: str
    name: str | None = None
    ts: datetime | None = None
    # 归一化纵坐标（相对整图高），供 OCR↔表情 Matcher 按行合并；不进提示词
    y_norm: float | None = None


class ScreenContext(BaseModel):
    app_id: str = "wechat_win"
    title: str = ""
    messages: list[ChatMessage] = Field(default_factory=list)
    source: ContextSource = "vision_api"


class Analysis(BaseModel):
    intent: str = ""
    need: str = ""
    suggested_action: str = ""
    risk: int = Field(default=0, ge=0, le=9)
    should_reply_now: bool = False
    confidence: float | None = None
    degraded: bool = False


class Candidate(BaseModel):
    text: str
    score: float | None = None
    recommended: bool = False


class AnalyzeResult(BaseModel):
    context: ScreenContext
    analysis: Analysis
    candidates: list[Candidate] = Field(default_factory=list)
    draft_error: str | None = None
    # 自动模式：对面无新话（例如刚发出的是自己），跳过起草
    skipped_reason: str | None = None
    # 墙钟拆分（毫秒），供 UI / 日志
    grab_ms: float = 0.0
    vision_ms: float = 0.0
    vision_source: str = ""  # api | cache
    llm_ms: float = 0.0
    total_ms: float = 0.0
