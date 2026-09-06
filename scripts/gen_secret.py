#!/usr/bin/env python3
"""Генератор SECRET_KEY для .env (шифрование Telethon-сессий)."""
from cryptography.fernet import Fernet

if __name__ == "__main__":
    print(Fernet.generate_key().decode())
