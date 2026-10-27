# Minecraft 업타임 디스코드 봇

디스코드 서버마다 Minecraft Java Edition 서버를 최대 10개까지 등록하고, 누구나 `!업타임`으로 상태를 확인할 수 있습니다.

## 명령어
- `!주소추가 hypixel.net 하이픽셀` — 서버 추가 (이름은 생략 가능, 누구나 사용 가능)
- `!업타임` — 등록된 모든 서버의 상태와 24시간/7일 업타임
- `!업타임 하이픽셀` — 서버 하나의 자세한 업타임 (이름 또는 주소)
- `!주소` — 등록된 서버 목록
- `!주소삭제 하이픽셀` — 서버와 기록 삭제 (서버 관리 권한 필요)
- `!H전적 hypixel` — 하이픽셀 전적 카드 (이미지). `bw`, `sw`, `duels`를 붙이면 게임별 전적
- `!V전적 Hide on bush#KR1` — 발로란트 랭크(아이콘), 최근 10경기 승률·K/D·헤드샷·ADR(데스매치 제외), 최근 5경기. 지역을 붙일 수 있음 (기본 `kr`, `ap`·`na`·`eu`·`latam`·`br`)
- `!도움말` — 명령어 안내

## 봇 설정
1. Discord Developer Portal에서 봇을 만들고 **Message Content Intent**를 켭니다.
2. OAuth2 URL Generator에서 `bot` 스코프와 아래 권한을 선택해 초대합니다.
   - View Channels, Send Messages, Embed Links, Attach Files, Read Message History (권한 값 `117760`)
   - 이미 초대한 봇은 서버 설정 → 역할에서 봇 역할에 **파일 첨부** 권한을 켜면 됩니다.

## 전적 API 키
- `HYPIXEL_API_KEY`: developer.hypixel.net에서 발급합니다. 개발용 키는 약 3일마다 만료되므로 계속 쓰려면 Personal 키를 신청하세요.
- `HENRIK_API_KEY`: HenrikDev 디스코드에 들어간 뒤 https://api.henrikdev.xyz/dashboard/ 에서 Basic 키를 발급합니다.
- 키는 `.env`(또는 호스팅의 환경변수)에만 넣고 GitHub에 올리지 마세요. 키가 없으면 해당 명령어만 안내 문구를 보여 줍니다.

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
- Java Edition 전용이며, 서버 목록은 디스코드 서버별로 따로 저장됩니다.
- 이전 버전(주소 1개)의 데이터는 실행 시 자동으로 옮겨지며, 이름은 주소로 설정됩니다.
- 업타임은 봇이 수집한 표본 기준이라 봇이 꺼진 시간은 측정되지 않습니다.
- 호스팅 IP가 차단되어 직접 조회가 실패하면 마지막 수단으로 외부 서비스(mcsrvstat.us)로 확인합니다. 도메인 주소만 전송하며 IP 주소는 절대 보내지 않습니다. 외부 조회 결과는 응답 시간이 없고 몇 분 캐시될 수 있습니다. `.env`의 `EXTERNAL_FALLBACK=false`로 끌 수 있습니다.
- 확인 간격은 `.env`의 `CHECK_INTERVAL_SECONDS`로 조절합니다. (기본 10초, 최소 1초)
- 전적 결과는 API 한도를 아끼기 위해 5분간 캐시되며, 같은 사용자는 5초에 한 번 조회할 수 있습니다.
- `!H전적`은 조회한 닉네임을 Mojang·Hypixel API로, 스킨 이미지를 위해 UUID를 crafatar.com(실패 시 mc-heads.net)으로 보냅니다. `!V전적`은 Riot ID를 HenrikDev API로 보냅니다.
- 카드 폰트는 [Monocraft](https://github.com/IdreesInc/Monocraft)(SIL Open Font License 1.1)이며 라이선스는 `assets/fonts/Monocraft-OFL.txt`에 있습니다.

#Invite it!
https://discord.com/oauth2/authorize?client_id=1561618276763570186&permissions=8515702525262144&integration_type=0&scope=bot
