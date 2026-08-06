"""
진단용 스크립트 - app.py와 무관하게 이것만 따로 실행해서 결과를 캡처해 보내주세요.
실행: python diagnose_naver.py
"""
import requests
from bs4 import BeautifulSoup

url = "https://finance.naver.com/sise/sise_deal_rank.naver?sosok=02&investor_gubun=9000"
headers = {"User-Agent": "Mozilla/5.0"}

res = requests.get(url, headers=headers, timeout=10)
res.encoding = res.apparent_encoding
soup = BeautifulSoup(res.text, 'html.parser')

# 1) type_5 클래스 테이블이 몇 개 있는지 확인
tables = soup.select('table.type_5')
print(f"\n=== table.type_5 개수: {len(tables)} ===")

# 2) 각 table.type_5 안에서 종목 링크가 몇 개, 어떤 순서로 나오는지 확인
for i, tb in enumerate(tables):
    links = [a.text.strip() for a in tb.find_all('a') if 'main.naver?code=' in a.get('href', '')]
    print(f"\n--- table {i} : 종목 링크 {len(links)}개 ---")
    print(links)

# 3) 페이지 전체에서 날짜 텍스트(예: 26.07.24) 위치 확인
import re
date_matches = re.findall(r'\d{2}\.\d{2}\.\d{2}', res.text)
print(f"\n=== 페이지에서 발견된 날짜 텍스트(중복 포함, 앞부분 20개): ===")
print(date_matches[:20])

# 4) 페이지 전체(사이드바 포함)에서 발견되는 전체 종목 링크 개수도 참고로 확인
all_links = [a.text.strip() for a in soup.find_all('a') if 'main.naver?code=' in a.get('href', '')]
print(f"\n=== 페이지 전체(사이드바 포함) 종목 링크 총 개수: {len(all_links)} ===")
print(all_links[:30])
