# Minecraft 업타임 디스코드 봇

디스코드 서버마다 Minecraft Java Edition 서버를 최대 10개까지 등록하고, 누구나 `!업타임`으로 상태를 확인할 수 있습니다.

## 명령어
- `!주소추가 hypixel.net 하이픽셀` — 서버 추가 (이름은 생략 가능, 누구나 사용 가능)
- `!업타임` — 등록된 모든 서버의 상태와 24시간/7일 업타임
- `!업타임 하이픽셀` — 서버 하나의 자세한 업타임 (이름 또는 주소)
- `!주소` — 등록된 서버 목록
- `!주소삭제 하이픽셀` — 서버와 기록 삭제 (서버 관리 권한 필요)
- `!H전적 hypixel` — 하이픽셀 전적 카드 (이미지). `bw`, `sw`, `duels`를 붙이면 게임별 전적
- `!V전적 Hide on bush#KR1` — 발로란트 전적 카드 (이미지). 랭크 아이콘·RR, 최고 랭크, 최근 10경기 승률·K/D·헤드샷·ADR(데스매치 제외), 최근 5경기. 지역은 계정에서 자동으로 찾으며 직접 붙일 수도 있음 (`kr`·`ap`·`na`·`eu`·`latam`·`br`). 계정을 못 찾으면 헷갈리기 쉬운 글자(I/l/1, O/0)를 바꾼 비슷한 ID를 버튼으로 제안
- `!개발자소통` — 개발자에게 메시지 보내기. 봇이 DM으로 물어보고, DM에 쓴 내용과 사진을 전달 (`/도움말`의 ✉️ DM to Dev.를 누르면 나만 보이는 입력 창: 종류·제목·내용·사진 3장). 개발자가 답장하면 DM으로 받고, DM의 [답장하기]로 다시 보낼 수 있음. 5분에 한 번
- `!운세` — 오늘의 운세 (칠 때마다 무작위). 문장은 `fortunes.txt`에 한 줄에 하나씩 있으며, 메모장으로 고치면 재시작 없이 바로 적용
- `!일침` — 정신이 번쩍 드는 한마디 (칠 때마다 무작위). 문장은 `ilchim.txt`
- `!도움말` — 명령어 안내
- `/도움말` — 나만 보이는 버튼 도움말 (메시지 1개). [서버 업타임]·[전적]·[기타(운세·일침)]·[DM to Dev.]·[명령어 보기(`!도움말`과 같은 전체 목록)]를 누르고, 명령어 버튼을 누르면 바로 실행 (주소추가·Hypixel 전적·Valorant 전적은 입력 창, 주소삭제는 목록에서 고르기). 업타임·전적 카드 같은 결과는 `!` 명령어처럼 채널에 보이고, `주소` 목록과 안내 문구는 도움말 메시지 안에 표시. 배너 이미지는 `assets/images/help_banner.jpg`(GitHub 주소로 표시, 바꾸려면 `.env`의 `HELP_BANNER_URL`), 전적 버튼 아이콘은 `assets/emojis/`(봇이 처음 켜질 때 봇 전용 이모지 `maco_hypixel`·`maco_valorant`로 자동 등록. 이미지를 바꾸면 Developer Portal → Emojis에서 지운 뒤 재시작)

## 봇 설정
1. Discord Developer Portal에서 봇을 만들고 **Message Content Intent**를 켭니다.
2. OAuth2 URL Generator에서 `bot`, `applications.commands` 스코프와 아래 권한을 선택해 초대합니다. (`applications.commands`는 `/도움말` 같은 슬래시 명령어용이며, 예전 링크로 초대한 서버는 새 링크로 다시 초대하면 됩니다. 다시 초대해도 봇이 나가지 않습니다.)
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
- API 키 만료·요청 한도 초과·외부 서비스 장애나 예상치 못한 오류가 나면 봇 관리자(`.env`의 `BOT_OWNER_IDS`, 비우면 봇을 만든 계정)에게 DM으로 알립니다. 같은 오류는 10분에 한 번만 보냅니다. 봇과 같은 서버에 있어야 DM을 받을 수 있습니다.
- 전적 결과는 API 한도를 아끼기 위해 5분간 캐시되며, 같은 사용자는 5초에 한 번 조회할 수 있습니다.
- `!H전적`은 조회한 닉네임을 Mojang·Hypixel API로, 스킨 이미지를 위해 UUID를 crafatar.com(실패 시 mc-heads.net)으로 보냅니다. `!V전적`은 Riot ID를 HenrikDev API로 보내고, 랭크·요원 아이콘을 media.valorant-api.com에서 받습니다(사용자 정보는 보내지 않음).
- 카드 폰트는 [Monocraft](https://github.com/IdreesInc/Monocraft)와 [Galmuri](https://github.com/quiple/galmuri)(둘 다 SIL Open Font License 1.1)이며 라이선스는 `assets/fonts/`에 있습니다.

#Invite it!
https://discord.com/oauth2/authorize?client_id=1561618276763570186&permissions=8515702525262144&integration_type=0&scope=bot%20applications.commands
