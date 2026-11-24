# 지도 그림 출처

지도 맞히기(`quiz.py`)의 그림입니다. `<코드>.png`는 주변이 보이는 위치 지도(정답 나라는 빨간색, 아주 작은 나라는 빨간 동그라미), `<코드>_s.png`는 나라 모양(실루엣)만 그린 것입니다.

- 국경선: [Natural Earth](https://www.naturalearthdata.com/) 1:10m 나라 경계 (퍼블릭 도메인), [world-atlas](https://github.com/topojson/world-atlas)의 TopoJSON으로 받아 Pillow로 미리 그렸습니다.
- ISO 번호가 없는 조각(소말릴란드, 북키프로스, 바이코누르 등)은 흔히 함께 표시되는 나라의 일부로 그렸습니다.
- 본토에서 멀리 떨어진 땅(알래스카, 프랑스령 기아나 등)은 그림 가운데에서 빼고, 본토 쪽을 보여 줍니다.
