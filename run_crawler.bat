@echo off
REM Thin shim - the Scheduled Task (StockBriefCrawler) points here. Logic lives in run_crawler.ps1.
REM ASCII ONLY - do NOT put Korean text in this file (it breaks cmd parsing and python never launches).
REM Do NOT put logic here either: cmd's %date% is "07-16(mok)" and the parentheses terminate
REM parenthesized blocks early, which is a syntax error. PowerShell has neither problem.
powershell -ExecutionPolicy Bypass -NoProfile -WindowStyle Hidden -File C:\ohai\stock-brief\run_crawler.ps1
exit /b %errorlevel%
