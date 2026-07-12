@echo off
cd /d C:\ohai\stock-brief
python crawler.py
python enrich.py
python financials.py
python notify_telegram.py
python publish.py
