"""Describe bounded message windows without implying missing neighbors exist."""
from .models import Message


def context_metadata(anchor: Message, window: list[Message], before: int, after: int) -> dict:
    position = next((i for i, msg in enumerate(window)
                     if msg.id == anchor.id and msg.conversation_id == anchor.conversation_id), None)
    left = position if position is not None else 0
    right = len(window) - position - 1 if position is not None else 0
    return {"requested_before": before, "requested_after": after,
            "returned_before": left, "returned_after": right,
            "partial": left < before or right < after or position is None}
