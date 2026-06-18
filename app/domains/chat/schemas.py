"""Chat 域请求体（Pydantic）。router 只用它做参数校验。"""
from pydantic import BaseModel, Field


class CreateConversationRequest(BaseModel):
    title: str = "新对话"


class SendMessageRequest(BaseModel):
    message: str = Field(..., min_length=1)


class VisionRequest(BaseModel):
    message: str = ""
    images: list[str] = Field(default_factory=list)  # dataURL / base64，最多取 3 张


class AnalyzeRequest(BaseModel):
    keyword: str = Field(..., min_length=1)
    type: str = "feasibility"  # blue_ocean / voc / feasibility / compare / listing / pricing
