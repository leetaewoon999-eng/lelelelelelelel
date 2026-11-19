# 부동산 매물 알림 (텔레그램)

지정 지역에서 **전용 84㎡ 이상 · 9억 이하 · 최근 실거래가보다 5% 이상 싼 아파트 매매 매물**이 올라오면 텔레그램으로 알려줍니다. 조건과 지역은 `config.yaml`에서 바꿉니다.

| 소스 | 상태 |
|---|---|
| 직방 | ✅ 동작 확인 (호갱노노는 직방 계열이라 매물 풀이 같음) |
| 당근 부동산 | ✅ 동작 확인 (동별 최신 20건, 직거래 위주) |
| 네이버 부동산 | ⚠️ 해외·클라우드 IP 차단 → 국내 PC에서 `sources.naver: true` |
| 실거래가 | 국토부 아파트 매매 실거래가 API (같은 단지·같은 전용면적의 최근 거래와 비교) |

## 준비물
1. **공공데이터포털 인증키**: data.go.kr → "국토교통부_아파트 매매 실거래가 자료" 활용신청 → 일반 인증키(Decoding)
2. **텔레그램 봇**: @BotFather → `/newbot` → 토큰 발급. 봇에게 아무 메시지 보낸 뒤
   `https://api.telegram.org/bot<토큰>/getUpdates` 에서 `chat.id` 확인

## A. GitHub Actions (PC 꺼져 있어도 동작)
1. 이 폴더를 GitHub 저장소에 올림 (**Public 저장소 권장**: 1회 약 5~7분 × 30분 간격이라 Private 무료 시간(월 2,000분)을 넘음. Private이면 cron을 2시간 간격으로)
2. Settings → Secrets and variables → Actions 에 `MOLIT_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` 등록
3. Actions 탭 → "부동산 매물 알림" → Run workflow 로 첫 실행

## B. 내 PC (네이버 포함)
```
pip install -r requirements.txt
python -m alert --dry-run          # 텔레그램 대신 화면 출력, 기록 저장 안 함
```
`run_local.bat`에 키를 넣고 Windows 작업 스케줄러에 30분 간격으로 등록.

## 동작 방식
- 첫 실행 때는 현재 조건에 맞는 매물을 모두 보냄 (15건 넘으면 나머지는 요약)
- 이후엔 **새로 조건에 맞게 된 매물**과 **알림 후 가격이 더 내린 매물**만 보냄
- 실거래 매칭: 지번(직방·당근) → 단지명 유사도 순. 최근 6개월 거래가 없으면 12개월까지 찾음
- 사이트 수집 실패 시 하루 한 번 경고 메시지
