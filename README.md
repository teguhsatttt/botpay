Payment Bot (only)
Setup:
- python3 -m venv venv && source venv/bin/activate
- pip install -r requirements.txt
Konfigurasi:
- Edit config.json: token bot, chat_id channel, QRIS (file_id/url/path), endpoint mutasi, token.
- Bot harus admin di channel (Invite Users + Ban Users).
Run:
- python3 bot_real_channel.py
