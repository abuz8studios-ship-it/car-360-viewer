import urllib.request
import re
import os

# Download page
url = 'https://www.drivesmartwc.com/inventory/mercedes-benz/gle/764948/'
req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
html = urllib.request.urlopen(req).read().decode('utf-8')

# Extract image URLs
pattern = r'https://imagescf\.dealercenter\.net/\d+/\d+/[^\s"<>]+\.jpg'
matches = re.findall(pattern, html)
unique = list(dict.fromkeys(matches))

print(f'Found {len(unique)} unique image URLs')

# Download first 12
base = r'E:\ABU\360Car\app\vehicles\764948\photos'
os.makedirs(base, exist_ok=True)

for i, img_url in enumerate(unique[:12], 1):
    fname = f'photo_{i:02d}.jpg'
    fpath = os.path.join(base, fname)
    try:
        urllib.request.urlretrieve(img_url, fpath)
        size = os.path.getsize(fpath)
        print(f'  Downloaded {i}: {fname} ({size//1024} KB)')
    except Exception as e:
        print(f'  Failed: {fname} - {e}')

print(f'\nTotal: {min(len(unique), 12)} photos saved')
