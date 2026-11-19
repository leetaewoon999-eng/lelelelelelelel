@echo off
chcp 65001 >nul
rem Windows PC에서 실행 (작업 스케줄러에 30분 간격으로 등록)
cd /d %~dp0
set MOLIT_API_KEY=여기에_공공데이터포털_인증키
set TELEGRAM_BOT_TOKEN=여기에_봇토큰
set TELEGRAM_CHAT_ID=여기에_채팅ID
python -m alert >> state\run.log 2>&1
