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
- `!올리기 [이름]` + 파일 첨부 — 파일을 저장소(Backblaze B2 등)에 올리고 링크를 보냄 (사진·영상은 채팅창에서 바로 보임, 한 번에 5개까지). 첨부 없이 쓰면 큰 파일(기본 500MB까지)용 업로드 페이지 링크를 DM으로 보내고, 다 올린 뒤 [업로드 완료]를 누르면 채널에 링크를 올림. 누구나 사용, 10초에 한 번
- `!파일 [이름]` — 이 서버에 올린 파일 목록 / 이름을 붙이면 그 파일 링크
- `!파일삭제 <이름>` — 올린 사람 또는 서버 관리 권한이 있는 사람 (봇 관리자는 어디서나)
- 올린 파일은 **7일(관리자가 `£DB 기간`으로 변경 가능) 후 자동 삭제**되며, 올릴 때마다 안내함
- `!운세` — 오늘의 운세 (칠 때마다 무작위). 문장은 `fortunes.txt`에 한 줄에 하나씩 있으며, 메모장으로 고치면 재시작 없이 바로 적용
- `!일침` — 정신이 번쩍 드는 한마디 (칠 때마다 무작위). 문장은 `ilchim.txt`
- `!도움말` — 명령어 안내
- `/도움말` — 나만 보이는 버튼 도움말 (메시지 1개). [서버 업타임]·[전적]·[파일]·[기타(운세·일침)]·[DM to Dev.]·[명령어 보기(`!도움말`과 같은 전체 목록)]를 누르고, 명령어 버튼을 누르면 바로 실행 (주소추가·Hypixel 전적·Valorant 전적은 입력 창, 주소삭제는 목록에서 고르기). 업타임·전적 카드 같은 결과는 `!` 명령어처럼 채널에 보이고, `주소` 목록과 안내 문구는 도움말 메시지 안에 표시. 배너 이미지는 `assets/images/help_banner.jpg`(GitHub 주소로 표시, 바꾸려면 `.env`의 `HELP_BANNER_URL`), 전적 버튼 아이콘은 `assets/emojis/`(봇이 처음 켜질 때 봇 전용 이모지 `maco_hypixel`·`maco_valorant`로 자동 등록. 이미지를 바꾸면 Developer Portal → Emojis에서 지운 뒤 재시작)

## 봇 설정
1. Discord Developer Portal에서 봇을 만들고 **Message Content Intent**를 켭니다.
2. OAuth2 URL Generator에서 `bot`, `applications.commands` 스코프와 아래 권한을 선택해 초대합니다. (`applications.commands`는 `/도움말` 같은 슬래시 명령어용이며, 예전 링크로 초대한 서버는 새 링크로 다시 초대하면 됩니다. 다시 초대해도 봇이 나가지 않습니다.)
   - View Channels, Send Messages, Embed Links, Attach Files, Read Message History (권한 값 `117760`)
   - 이미 초대한 봇은 서버 설정 → 역할에서 봇 역할에 **파일 첨부** 권한을 켜면 됩니다.

## 전적 API 키
- `HYPIXEL_API_KEY`: developer.hypixel.net에서 발급합니다. 개발용 키는 약 3일마다 만료되므로 계속 쓰려면 Personal 키를 신청하세요.
- `HENRIK_API_KEY`: HenrikDev 디스코드에 들어간 뒤 https://api.henrikdev.xyz/dashboard/ 에서 Basic 키를 발급합니다.
- 키는 `.env`(또는 호스팅의 환경변수)에만 넣고 GitHub에 올리지 마세요. 키가 없으면 해당 명령어만 안내 문구를 보여 줍니다.

## 파일 저장소 (Backblaze B2 추천, 카드 없이 무료 10GB)
S3 호환 저장소라면 어디든 쓸 수 있습니다. 아래는 Backblaze B2 기준입니다.
1. https://www.backblaze.com 에서 **B2 Cloud Storage**로 가입합니다. 가입 버튼 아래의 지역을 **US West**로 고릅니다. (나중에 바꿀 수 없음)
2. **Buckets → Create a Bucket**에서 버킷을 만듭니다. **Files in Bucket are: Private**(비공개)로 두면 됩니다. 봇이 7일짜리 서명된 링크를 만들어 보내므로 공개 버킷이 필요 없습니다. (B2는 공개 버킷에 결제 이력을 요구합니다)
3. 버킷의 **Lifecycle Settings**를 **Keep only the last version of the file**로 바꿉니다. B2는 지운 파일을 숨김 버전으로 남겨 두기 때문입니다. (봇도 지울 때 숨김 버전까지 지웁니다)
4. 버킷 정보의 **Endpoint**(예: `s3.us-west-004.backblazeb2.com`)를 확인합니다.
5. **Application Keys → Add a New Application Key**에서 이 버킷만, **Read and Write** 권한으로 키를 만듭니다. `keyID`와 `applicationKey`는 한 번만 보여 주니 바로 복사합니다.
6. `.env`에 넣고 봇을 재시작합니다.
   ```
   STORAGE_ENDPOINT=https://s3.us-west-004.backblazeb2.com
   STORAGE_ACCESS_KEY_ID=keyID
   STORAGE_SECRET_ACCESS_KEY=applicationKey
   STORAGE_BUCKET=버킷이름
   ```
- 봇이 켜질 때 큰 파일 업로드 페이지(`assets/upload.html`)를 버킷에 올립니다. 비공개 버킷에서는 페이지와 업로드가 같은 주소라 CORS 설정이 필요 없습니다.
- B2 무료 범위: 저장 10GB, 다운로드는 저장량의 3배까지, 하루 다운로드 요청 2,500회 등. 카드가 없으면 넘었을 때 요금 대신 막힙니다.
- 다른 저장소: 공개 버킷을 쓰려면 `STORAGE_PUBLIC_URL`에 공개 주소를 넣습니다(이때 CORS가 필요하며 봇이 설정을 시도합니다). Cloudflare R2는 예전 설정(`R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`, `R2_PUBLIC_URL`)도 그대로 동작합니다.
- 사진·영상·음악은 바로 보이게, 그 밖의 파일(HTML 등)은 열리지 않고 다운로드만 되게 저장합니다.
- 영상 변환은 기본으로 꺼져 있습니다. `.env`에 `FILE_CONVERT_VIDEO=true`를 넣고 호스팅에 `ffmpeg`가 있으면 아이폰 영상(MOV·HEVC) 등 디스코드에서 재생되지 않는 영상을 MP4(H.264)로 바꿔서 올립니다. 변환은 메모리를 많이 써서 128MB 호스팅에서는 켜지 않는 것을 권장합니다.
- 파일은 메모리에 통째로 담지 않고 조금씩 저장소로 흘려보내므로, 큰 파일을 올려도 봇의 메모리 사용량은 거의 늘지 않습니다.
- 한도: 서버당 `FILE_GUILD_QUOTA_MB`, 전체 `FILE_TOTAL_QUOTA_MB`(기본 각 10GB), 파일당 `FILE_MAX_MB`(기본 500MB). 디스코드 첨부는 서버 부스트에 따라 더 작은 제한이 있어 큰 파일은 업로드 페이지를 씁니다.

## 메모리 (128MB 호스팅)
- 봇은 메모리를 아끼도록 설정되어 있습니다: 저장소 통신을 boto3 없이 직접 처리, 통신 세션 공유, 디스코드 메시지 캐시 끔, 작업 스레드 2개, 메모리 영역(arena) 2개로 제한.
- `£DB`의 🧠 메모리에서 지금 사용량과 켜진 뒤 최대치를 볼 수 있습니다. 호스팅 한도가 128MB가 아니면 `.env`의 `MEMORY_LIMIT_MB`로 맞춥니다.

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
- 봇 관리자 전용 명령어 (다른 사람이 치면 아무 반응 없음). `£도움말`로 목록을 볼 수 있습니다.
  - `£servers` — 참여 중인 서버와 24시간 메시지·명령어 수
  - `£DB` — 로컬 DB와 파일 저장소 상태 (연결·응답 시간, 사용/남은 용량, 파일 수, 다음 자동 삭제 등)
  - `£DB 파일 [서버ID]` — 올라간 파일과 번호 · `£DB 삭제 <번호...>` — 번호로 삭제 · `£DB 서버삭제 <서버ID>` — 그 서버 파일 전부 삭제(확인 버튼)
  - `£DB 기간 [기간]` — 자동 삭제 기간 보기/바꾸기 (기본 7일, 1시간~7일, 공개 저장소는 30일까지). 새로 올리는 파일부터 적용
  - `£DB 정리` — 기간 지난 파일 즉시 삭제 + 목록에 없이 저장소에만 남은 파일 정리
- 전적 결과는 API 한도를 아끼기 위해 5분간 캐시되며, 같은 사용자는 5초에 한 번 조회할 수 있습니다.
- `!H전적`은 조회한 닉네임을 Mojang·Hypixel API로, 스킨 이미지를 위해 UUID를 crafatar.com(실패 시 mc-heads.net)으로 보냅니다. `!V전적`은 Riot ID를 HenrikDev API로 보내고, 랭크·요원 아이콘을 media.valorant-api.com에서 받습니다(사용자 정보는 보내지 않음).
- 카드 폰트는 [Monocraft](https://github.com/IdreesInc/Monocraft)와 [Galmuri](https://github.com/quiple/galmuri)(둘 다 SIL Open Font License 1.1)이며 라이선스는 `assets/fonts/`에 있습니다.

#Invite it!
https://discord.com/oauth2/authorize?client_id=1561618276763570186&permissions=8515702525262144&integration_type=0&scope=bot%20applications.commands
