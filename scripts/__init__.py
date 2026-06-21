"""Puntos de entrada por línea de comandos para los ciclos automatizados de MAREA.

Estos scripts los ejecuta GitHub Actions por cron. NO levantan FastAPI:
importan y ejecutan los engines internos directamente, en orden, reutilizando
la misma lógica que sirven los endpoints HTTP de ``app/main.py``.
"""
