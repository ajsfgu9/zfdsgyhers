"""Re-exports TelegramBotBridge and TelegramBotService from `ouroboros.telegram_bot`."""

from ouroboros.telegram_bot import (
    MAX_TELEGRAM_MSG_LEN,
    PendingApproval,
    TelegramBotBridge,
    TelegramBotService,
)

__all__ = [
    "MAX_TELEGRAM_MSG_LEN",
    "PendingApproval",
    "TelegramBotBridge",
    "TelegramBotService",
]
