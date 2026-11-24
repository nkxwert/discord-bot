# countries.json 출처

나라 맞히기(`nara.py`)가 쓰는 197개 나라(UN 회원국 + 대만, 코소보, 팔레스타인, 바티칸) 데이터입니다.

- 나라 이름(한글·영어·별칭), 대륙, 면적, 내륙국 여부: [world-countries](https://github.com/mledoze/countries) (Mohammed Le Doze, ODbL 1.0)
- 국기, 이웃 나라, 언어·통화 코드: world-countries
- 수도·언어·통화의 한글 이름, 한국어 위키백과 문서 이름: [Wikidata](https://www.wikidata.org/) (CC0)
- 인구(가장 최근 해), 수도 좌표: [World Bank Open Data](https://data.worldbank.org/) (CC BY 4.0)
- 대만·바티칸의 인구와 수도 좌표, 팔레스타인의 수도 좌표, 일부 한글 이름과 별칭은 직접 보탰습니다.
- `!정보`의 소개글은 데이터에 넣지 않고, 쓸 때 한국어 위키백과(CC BY-SA)에서 받아 출처와 함께 보여 줍니다.

This file is a Database (countries.json) made available under the Open Database License (ODbL 1.0),
built from world-countries (ODbL) and World Bank data (CC BY 4.0).
- 국기 맞히기의 국기 그림(`assets/flags/*.png`, 가로 640px): [flagcdn.com](https://flagcdn.com/) (국기 그림은 퍼블릭 도메인)
- 수도 맞히기의 정답 표기(`capitals`, `capital_names`): Wikidata의 수도 이름(한글·영어)과 world-countries의 영어 이름에, 흔히 쓰는 다른 표기(북경, 동경, 타이페이 등)와 수도가 여럿인 나라의 목록을 직접 보탰습니다. 평양·도쿄·베이징·쿠웨이트시티·야렌은 흔히 쓰는 이름으로 표시합니다.
- 평균 기온(`temp`, 1991~2020년 평균): World Bank Climate Change Knowledge Portal 값(영어 위키백과 "List of countries by average yearly temperature"에 정리된 것). 브루나이·대만은 그 목록에 없어 비슷한 자료의 대략값을 넣었습니다.
