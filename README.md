# 공용 Minecraft Uptime Discord Bot

각 디스코드 서버(guild)마다 공용 Minecraft Java Edition 주소 하나를 저장합니다. 한 사람이 주소를 등록하면 해당 디스코드 서버의 모든 멤버가 `!업타임`으로 같은 서버를 조회할 수 있습니다.

## 명령어
- `!주소설정 hypixel.net` — 현재 디스코드 서버의 공용 주소 등록/변경
- `!업타임` — 누구나 공용 서버 상태, 응답 시간, 최근 24시간/7일 관측 업타임 조회
- `!주소` — 공용 주소 확인
- `!주소삭제` — 서버 관리 권한이 있는 사용자만 공용 주소 삭제
- `!도움말` — 명령어 안내

## Windows 실행
1. Python 3.11 이상 설치
2. Discord Developer Portal에서 애플리케이션/봇 생성
3. Bot 설정에서 **Message Content Intent** 활성화
4. OAuth2 URL Generator에서 `bot` 권한과 `Send Messages`, `Embed Links` 권한을 선택해 봇 초대
5. 프로젝트 폴더에서 PowerShell 실행:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
notepad .env
python bot.py
```

`.env`의 `DISCORD_TOKEN`에 봇 토큰을 입력하고 저장한 다음 `python bot.py`를 실행합니다. 토큰을 공유하거나 GitHub에 올리지 마세요.

## 참고
- Minecraft **Java Edition** 상태 조회용입니다.
- 서버 주소는 디스코드 서버별로 공유되므로 한 디스코드 서버에 등록한 주소는 다른 디스코드 서버와 자동 공유되지 않습니다.
- 업타임은 봇이 주기적으로 수집한 상태 표본을 바탕으로 계산합니다. 봇이 꺼져 있던 시간은 정확히 측정할 수 없습니다.
- 상태 확인 간격 기본값은 60초이며 `.env`의 `CHECK_INTERVAL_SECONDS`로 조절할 수 있습니다(최소 30초).
- `!주소삭제`는 서버 관리 권한이 필요합니다.
