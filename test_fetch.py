import requests
from bs4 import BeautifulSoup
import sys

code = sys.argv[1] if len(sys.argv) > 1 else '005930'
url = f"https://finance.naver.com/item/main.naver?code={code}"
headers = {"User-Agent": "Mozilla/5.0"}

print('Fetching', url)
res = requests.get(url, headers=headers, timeout=10)
print('requests detected encoding:', res.encoding)
# try different decodings
for enc in ['utf-8', 'euc-kr', 'cp949']:
    try:
        text = res.content.decode(enc)
        soup = BeautifulSoup(text, 'html.parser')
        name_elem = soup.select_one('.wrap_company h2 a')
        name = name_elem.text.strip() if name_elem else '[NOT FOUND]'
        print(f'encoding={enc} -> parsed name: {name}')
    except Exception as e:
        print(f'encoding={enc} -> error: {e}')

# also print raw bytes sample
print('\nRaw bytes sample (first 200 bytes):')
print(res.content[:200])
