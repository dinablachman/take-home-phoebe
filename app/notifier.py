import asyncio
import logging


async def send_sms(phone_number: str, message: str) -> None:
    """
    Stubbed SMS sending function (would call Twilio API).

    Do not implement this.
    """
    await asyncio.sleep(1)
    logging.info(f"Sending SMS to {phone_number}: {message}")


async def place_phone_call(phone_number: str, message: str) -> None:
    """
    Stubbed call placing function (would call Twilio API).

    Do not implement this.
    """
    await asyncio.sleep(2)
    logging.info(f"Placing call to {phone_number}: {message}")
