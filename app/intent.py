from enum import StrEnum, auto


class ShiftRequestMessageIntent(StrEnum):
    ACCEPT = auto()
    DECLINE = auto()
    UNKNOWN = auto()


async def parse_shift_request_message_intent(
    message: str,
) -> ShiftRequestMessageIntent:
    """
    Parse the intent of an inbound caregiver message responding to a shift
    request. In practice, this would be a call to an LLM to determine the intent
    of the message.

    Do not re-implement this function.
    """
    normalized = message.strip().lower()
    if normalized.startswith(("yes", "accept")):
        return ShiftRequestMessageIntent.ACCEPT
    if normalized.startswith(("no", "decline")):
        return ShiftRequestMessageIntent.DECLINE
    return ShiftRequestMessageIntent.UNKNOWN
