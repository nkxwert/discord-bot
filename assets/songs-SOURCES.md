# songs.json 출처

노래 맞히기(`quiz.py`, `bot.py`)의 기본 곡 목록입니다. 166곡 (K-POP 98 · J-POP·애니 29 · 팝송 39).

- 곡 번호(Deezer track id), 제목, 가수, 앨범 표지 주소: [Deezer API](https://developers.deezer.com/api)
- 정답으로 받는 한글 제목·다른 표기(예: 하입보이, 밤편지): 직접 보탰습니다.
- 노래 파일은 저장하지 않습니다. 문제를 낼 때마다 Deezer의 공식 30초 미리듣기를 받아 몇 초만 잘라 올리고 바로 지우며, 정답과 함께 Deezer 원곡 링크를 보여 줍니다.
- 서버에서 `!노래추가`로 넣은 곡은 봇 DB(`song_custom`)에 곡 정보만 저장됩니다.
