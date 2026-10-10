"""The Daily Content bot: a separate Telegram bot for buyers with 15+ purchases.

Run as its own process (``python -m app.daily_bot``) with its own token. It
shares the database, Redis and the services of the shop, and nothing of the
shop bot's behaviour.
"""
