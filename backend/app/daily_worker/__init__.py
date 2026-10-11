"""The Daily Content worker: deliveries, alerts and notices, on their own schedule.

Run as its own process (``python -m app.daily_worker``) next to the Daily bot.
It shares the database, Redis and the services of the shop; it talks to
Telegram only to send, and serves nothing but its health check.
"""
