"""Small structured-output LLM helpers: emotion detection and return-form extraction."""

import json
import logging
from typing import Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage
from pydantic import BaseModel

from cs_agent.agent.prompts import EMOTION_SYSTEM, RETURN_SLOTS_SYSTEM

log = logging.getLogger(__name__)

# Internal calls are tagged so the streaming layer never shows their output.
INTERNAL_TAG = "internal"


class EmotionResult(BaseModel):
    emotion: Literal["calm", "frustrated", "angry"] = "calm"
    wants_human: bool = False


class ReturnSlots(BaseModel):
    order_id: str | None = None
    item_ids: list[int] | None = None
    reason: Literal["defective", "damaged_in_transit", "wrong_item", "not_as_described",
                    "changed_mind", "other"] | None = None
    comment: str | None = None
    refund_method: Literal["original", "balance"] | None = None
    wants_to_stop: bool = False
    off_topic: bool = False


def _transcript(messages: list[AnyMessage], limit: int = 6) -> str:
    lines = []
    for m in messages[-limit:]:
        if m.type in ("human", "ai") and m.text:
            lines.append(f"{'Customer' if m.type == 'human' else 'Assistant'}: {str(m.text)}")
    return "\n".join(lines)


def make_emotion_classifier(model: BaseChatModel):
    structured = model.with_structured_output(EmotionResult, method="json_mode").with_config(
        tags=[INTERNAL_TAG]
    )

    def classify(messages: list[AnyMessage]) -> EmotionResult:
        try:
            return structured.invoke(
                [SystemMessage(EMOTION_SYSTEM), HumanMessage(_transcript(messages))]
            )
        except Exception:
            log.exception("Emotion classification failed; assuming calm")
            return EmotionResult()

    return classify


def make_return_slot_extractor(model: BaseChatModel):
    structured = model.with_structured_output(ReturnSlots, method="json_mode").with_config(
        tags=[INTERNAL_TAG]
    )

    def extract(awaiting: str, options: list[dict], reply: str) -> ReturnSlots:
        system = RETURN_SLOTS_SYSTEM.format(
            awaiting=awaiting, options=json.dumps(options, ensure_ascii=False) or "[]"
        )
        try:
            return structured.invoke([SystemMessage(system), HumanMessage(reply)])
        except Exception:
            log.exception("Return slot extraction failed")
            return ReturnSlots()

    return extract
