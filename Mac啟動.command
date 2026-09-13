#!/bin/bash
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
    echo "首次使用，正在自動建立 Python 環境與安裝套件，請稍候..."
    python3 -m venv .venv
    source .venv/bin/activate
    pip install --upgrade pip
    pip install -r requirements.txt
    python -m playwright install chromium
else
    source .venv/bin/activate
fi

echo "正在啟動 E等公務員學習助手..."
python main.py
