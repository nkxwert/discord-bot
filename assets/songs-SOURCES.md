# songs.json 출처

노래 맞히기(`quiz.py`, `bot.py`)의 기본 곡 목록입니다. 166곡 (K-POP 98 · J-POP·애니 29 · 팝송 39).

- 곡 번호(Deezer track id, Apple의 trackId), 제목, 가수, 앨범 표지 주소: [Deezer API](https://developers.deezer.com/api), [iTunes Search API](https://performance-partners.apple.com/search-api)
- 정답으로 받는 한글 제목·다른 표기(예: 하입보이, 밤편지): 직접 보탰습니다.
- 노래 파일은 저장하지 않습니다. 문제를 낼 때마다 Deezer(안 되면 Apple)의 공식 30초 미리듣기를 받아 몇 초만 잘라 올리고, 정답 때 30초 전체를 재생할 수 있게 올린 뒤 바로 지웁니다. Deezer는 한국에서 서비스되지 않아 Apple 미리듣기를 예비로 씁니다.
- 서버에서 `!노래추가`로 넣은 곡은 봇 DB(`song_custom`)에 곡 정보만 저장됩니다.
