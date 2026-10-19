# Minecraft 업타임 디스코드 봇

디스코드 서버마다 Minecraft Java Edition 주소 하나를 저장하고, 누구나 `!업타임`으로 상태를 확인할 수 있습니다.

## 명령어
- `!주소설정 hypixel.net` — 주소 등록/변경 (누구나 사용 가능)
- `!업타임` — 상태, 응답 시간, 24시간/7일 업타임
- `!주소` — 등록된 주소 확인
- `!주소삭제` — 주소와 기록 삭제 (서버 관리 권한 필요)
- `!도움말` — 명령어 안내

## 봇 설정
1. Discord Developer Portal에서 봇을 만들고 **Message Content Intent**를 켭니다.
2. OAuth2 URL Generator에서 `bot` 스코프와 아래 권한을 선택해 초대합니다.
   - View Channels, Send Messages, Embed Links, Read Message History (권한 값 `84992`)

## Windows 실행
Python 3.11 이상이 필요합니다. PowerShell에서 실행합니다.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
notepad .env
python bot.py
```

`.env`의 `DISCORD_TOKEN`에 봇 토큰을 입력합니다. 토큰은 공유하거나 GitHub에 올리지 마세요.

## 참고
- Java Edition 전용이며, 주소는 디스코드 서버별로 따로 저장됩니다.
- 업타임은 봇이 수집한 표본 기준이라 봇이 꺼진 시간은 측정되지 않습니다.
- 확인 간격은 `.env`의 `CHECK_INTERVAL_SECONDS`로 조절합니다. (기본 10초, 최소 1초)
