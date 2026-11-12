"""Quick Dashboard data collector. Run locally or in GitHub Actions.
Never fabricate retail rates or replace original source timestamps with collection time.
"""
import csv, io, json, re, datetime as dt
from pathlib import Path
import requests
from bs4 import BeautifulSoup

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'market-data.json'
now=dt.datetime.now(dt.timezone.utc)
MY_TZ=dt.timezone(dt.timedelta(hours=8))
HEAD={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0 Safari/537.36','Accept-Language':'en-US,en;q=0.9'}
s=requests.Session();s.headers.update(HEAD)
try: old=json.loads(OUT.read_text(encoding='utf-8'))
except Exception: old={}
result={'generated_at':now.isoformat(),'values':{},'errors':{}}

def get(url,timeout=22):
 r=s.get(url,timeout=timeout);r.raise_for_status();return r

def save(key,value,source,asof,**extra):
 result['values'][key]={'value':value,'source':source,'asof':asof,**extra}

def attempt(key,fn):
 try: fn()
 except Exception as e:result['errors'][key]=str(e)[:250]

def extract_bse_sensex(payload):
 """Read the official BSE Sensex quote. Reject unrelated index rows or ambiguous data."""
 if isinstance(payload,str):
  payload=json.loads(payload)
 def walk(obj):
  if isinstance(obj,dict):
   yield obj
   for v in obj.values():
    if isinstance(v,(dict,list)):yield from walk(v)
  elif isinstance(obj,list):
   for v in obj:yield from walk(v)
 def norm(key):return re.sub(r'[^a-z0-9]','',str(key).lower().replace('%','pct'))
 def field(row,*names):
  lookup={norm(k):v for k,v in row.items()}
  for name in names:
   v=lookup.get(norm(name))
   if v is not None and str(v).strip() not in ('','--','-'):return v
  return None
 def number(value):
  if value is None:return None
  if isinstance(value,(int,float)):return float(value)
  v=re.sub(r'[,₹%\s]','',str(value))
  try:return float(v)
  except ValueError:return None
 def bse_time(value):
  if value is None:raise ValueError('BSE Sensex source timestamp missing')
  raw=str(value).strip()
  # BSE's displayed clock is India Standard Time, not UTC.
  raw=re.sub(r'\s*\|\s*',' ',raw)
  for fmt in ('%d %b %y %H:%M:%S','%d %b %y %H:%M','%d %b %Y %H:%M:%S',
              '%d %b %Y %H:%M','%d %B %Y %H:%M:%S','%d %B %Y %H:%M',
              '%d/%m/%Y %H:%M:%S','%d/%m/%Y %H:%M','%Y-%m-%d %H:%M:%S',
              '%Y-%m-%dT%H:%M:%S'):
   try:return dt.datetime.strptime(raw,fmt).replace(tzinfo=dt.timezone(dt.timedelta(hours=5,minutes=30))).isoformat()
   except ValueError:pass
  try:
   parsed=dt.datetime.fromisoformat(raw.replace('Z','+00:00'))
   if parsed.tzinfo:return parsed.isoformat()
  except ValueError:pass
  raise ValueError('BSE Sensex timestamp not recognised: '+raw[:65])
 rows=list(walk(payload))
 for row in rows:
  name=field(row,'Index','IndexName','Index Name','name','indxnm')
  if not name or norm(name) not in ('sensexvalue','sensex','bsesensex','spbsesensex'):
   continue
  price=number(field(row,'LTP','CurrentValue','Current Index Value','IndexValue','value','ltp'))
  if price is None or not 10000<price<200000:continue
  change=number(field(row,'Change','Chg','NetChange','PointChange','chg'))
  pct=number(field(row,'Change %','Change%','ChangePct','PercentChange','PChange','PerChange','perchg'))
  prev=number(field(row,'Prev Close','PrevClose','PreviousClose','Prev_Close'))
  if change is None and prev and 10000<prev<200000:change=price-prev
  if pct is None and prev and change is not None:pct=100*change/prev
  if pct is not None and abs(pct)>10:raise ValueError('BSE Sensex daily change failed sanity check')
  timestamp=field(row,'DateTime','Date Time','LastUpdated','LastUpdate','AsOn','TimeStamp','dttm')
  asof=bse_time(timestamp)
  return {'price':price,'change':change,'pct':pct},asof
 raise ValueError('Official BSE Sensex quote not found in response')

def sensex():
 url='https://api.bseindia.com/RealTimeBseIndiaAPI/api/GetSensexData/w'
 response=s.get(url,headers={'Referer':'https://www.bseindia.com/','Origin':'https://www.bseindia.com','Accept':'application/json, text/plain, */*'},timeout=15)
 response.raise_for_status()
 quote,asof=extract_bse_sensex(response.json())
 save('sensex',quote,'BSE India official Sensex quote',asof,url='https://www.bseindia.com/')

def extract_xe_myr_inr(html):
 """Read XE's displayed 1 MYR to INR mid-market quote; never infer from another provider."""
 soup=BeautifulSoup(html,'html.parser')
 for tag in soup(['script','style','noscript']):
  tag.decompose()
 text=re.sub(r'\s+',' ',soup.get_text(' ',strip=True).replace('\xa0',' '))
 # XE commonly renders: '1 MYR = 23.1234 INR' or '1 Malaysian Ringgit = ... Indian Rupees'.
 patterns=[
  r'1\s*(?:MYR|Malaysian\s+Ringgit)\s*(?:=|equals|is equal to)\s*(?:₹|INR)?\s*([0-9]{1,2}(?:\.[0-9]{2,8})?)\s*(?:INR|Indian\s+Rupees)?',
  r'1\s*(?:MYR|Malaysian\s+Ringgit)\s*(?:to|→)\s*INR.{0,100}?([0-9]{2}\.[0-9]{2,8})\s*(?:INR|Indian\s+Rupees)',
  r'([0-9]{2}\.[0-9]{2,8})\s*(?:INR|Indian\s+Rupees)\s*(?:per|for)\s*1\s*MYR',
 ]
 for pattern in patterns:
  for match in re.finditer(pattern,text,re.I):
   rate=float(match.group(1))
   if 10<rate<40:return rate
 # Some XE deployments embed the displayed conversion inside JSON rather than visible text.
 # Require adjacent currency markers and a known rate key; avoid arbitrary numbers on the page.
 for m in re.finditer(r'(?i)\"(?:midMarketRate|mid_market_rate|conversionRate|exchangeRate)\"\s*:\s*(?:\"|)([0-9]+(?:\.[0-9]+)?)',html):
  rate=float(m.group(1))
  context=html[max(0,m.start()-1500):m.end()+1500].upper()
  if 10<rate<40 and 'MYR' in context and 'INR' in context:return rate
 raise ValueError('XE MYR/INR quote not found in returned HTML (XE may block automated requests or render the rate in JavaScript)')

def exchange():
 url='https://www.xe.com/currencyconverter/convert/?Amount=1&From=MYR&To=INR'
 response=get(url,timeout=15)
 rate=extract_xe_myr_inr(response.text)
 # XE displays a dynamic mid-market rate. Record retrieval time, not a fabricated source timestamp.
 save('fx',{'inr':rate,'usd_per_myr':None},'XE MYR to INR displayed mid-market quote',
      dt.datetime.now(dt.timezone.utc).isoformat(),url=url)

def extract_goodreturns_malaysia(html):
 """Extract only Malaysia MYR per-gram prices, accepting NBSP and markup changes."""
 soup=BeautifulSoup(html,'html.parser')
 text=re.sub(r'\s+', ' ', soup.get_text(' ',strip=True).replace('\xa0',' '))
 if not re.search(r'Gold Rate in Malaysia',text,re.I):
  raise ValueError('Goodreturns Malaysia gold heading not present (possibly blocked page)')
 # The lead sentence on Goodreturns contains all three carats, with optional
 # spaces and nonbreaking spaces between RM and the number.
 lead=re.search(r'(?:Today[’\']?s gold price in Malaysia|gold price in Malaysia stands at)(.{0,850})',text,re.I)
 rates={}
 if lead:
  intro=lead.group(1)
  for karat in (24,22,18):
   pattern=rf'RM\s*([\d,]+(?:\.\d+)?)\s*per\s*gram\s*for\s*{karat}\s*(?:karat|carat|k)'
   m=re.search(pattern,intro,re.I)
   if m:rates[karat]=float(m.group(1).replace(',',''))
 # Fallback: cards or the 1-gram row, e.g. "24K Gold /g RM 537".
 if len(rates)!=3:
  section=re.split(r'Gold Rate in Malaysia',text,maxsplit=1,flags=re.I)[-1][:2500]
  for karat in (24,22,18):
   if karat in rates:continue
   m=re.search(rf'{karat}\s*K\s*Gold\s*/\s*g\s*RM\s*([\d,]+(?:\.\d+)?)',section,re.I)
   if m:rates[karat]=float(m.group(1).replace(',',''))
 if len(rates)!=3:raise ValueError('Goodreturns Malaysia 24K/22K/18K prices not found in returned HTML; site may serve different content to Python')
 if not (100<rates[18]<rates[22]<rates[24]<2000):raise ValueError('Goodreturns gold values failed validation')
 # Find date close to the gold heading, not unrelated dates elsewhere.
 section=re.split(r'Gold Rate in Malaysia',text,maxsplit=1,flags=re.I)[-1][:2000]
 dm=re.search(r'\b(\d{1,2})\s+([A-Za-z]+)\s+(20\d{2})\b',section)
 if not dm:raise ValueError('Goodreturns Malaysia source date missing')
 asof=dt.datetime.strptime(dm.group(0),'%d %B %Y').date()
 return rates,asof

def malaysia_gold_goodreturns():
 url='https://www.goodreturns.in/gold-rates/malaysia.html'
 response=get(url,timeout=15)
 rates,asof=extract_goodreturns_malaysia(response.text)
 age=(now.astimezone(MY_TZ).date()-asof).days
 if age<0 or age>2:raise ValueError(f'Goodreturns Malaysia rate is outdated: {asof}')
 save('gold_my',{'k24':rates[24],'k22':rates[22],'k18':rates[18]},
      'Goodreturns Malaysia indicative gold rates',asof.isoformat(),url=url)

def goodreturns_vijayawada_text(html, metal):
 soup=BeautifulSoup(html,'html.parser')
 text=re.sub(r'\s+',' ',soup.get_text(' ',strip=True).replace('\xa0',' '))
 # Use the last page heading match: the first may occur in SEO/navigation text.
 matches=list(re.finditer(rf'{metal} Rate in Vijayawada',text,re.I))
 if not matches:raise ValueError(f'Goodreturns Vijayawada {metal.lower()} heading missing')
 section=text[matches[0].end():]
 dm=re.search(r'\b(\d{1,2})\s+([A-Za-z]+)\s+(20\d{2})\b',section[:3000])
 if not dm:raise ValueError(f'Goodreturns Vijayawada {metal.lower()} date missing')
 asof=dt.datetime.strptime(dm.group(0),'%d %B %Y').date()
 age=(now.astimezone(dt.timezone(dt.timedelta(hours=5,minutes=30))).date()-asof).days
 if age<0 or age>2:raise ValueError(f'Goodreturns Vijayawada {metal.lower()} rate outdated: {asof}')
 return section,asof

def india_gold():
 url='https://www.goodreturns.in/gold-rates/vijayawada.html'
 section,asof=goodreturns_vijayawada_text(get(url,timeout=15).text,'Gold')
 rates={}
 # Parse the visible rate cards first, then the introductory text.
 for k in (24,22):
  patterns=[
   rf'{k}\s*K\s*Gold\s*/\s*g\s*₹\s*([\d,]+(?:\.\d+)?)',
   rf'₹\s*([\d,]+(?:\.\d+)?)\s*per\s*gram\s*for\s*{k}\s*(?:karat|carat|k)',
   rf'{k}\s*K\s*Gold[^₹]{{0,30}}₹\s*([\d,]+(?:\.\d+)?)'
  ]
  for pattern in patterns:
   m=re.search(pattern,section[:3500],re.I)
   if m:
    rates[k]=float(m.group(1).replace(',',''));break
 if len(rates)!=2 or not 1000<rates[22]<rates[24]<50000:
  raise ValueError('Goodreturns Vijayawada gold 24K/22K per-gram rates missing or invalid')
 save('gold_in',{'k24':rates[24],'k22':rates[22]},'Goodreturns Vijayawada indicative gold rates',asof.isoformat(),url=url)

def silver_india():
 url='https://www.goodreturns.in/silver-rates/vijayawada.html'
 section,asof=goodreturns_vijayawada_text(get(url,timeout=15).text,'Silver')
 def extract(patterns):
  for pattern in patterns:
   m=re.search(pattern,section[:3500],re.I)
   if m:return float(m.group(1).replace(',',''))
  return None
 g=extract([r'Silver\s*/\s*g\s*₹\s*([\d,]+(?:\.\d+)?)',
            r'₹\s*([\d,]+(?:\.\d+)?)\s*per\s*gram'])
 k=extract([r'Silver\s*/\s*kg\s*₹\s*([\d,]+(?:\.\d+)?)',
            r'₹\s*([\d,]+(?:\.\d+)?)\s*per\s*kilogram'])
 if g is None or k is None:raise ValueError('Goodreturns Vijayawada silver gram/kg rates missing')
 if not 20<g<1000 or abs(k-g*1000)>max(1,g*1000*.01):
  raise ValueError('Goodreturns Vijayawada silver units failed validation')
 save('silver_in',{'gram':g,'kg':k},'Goodreturns Vijayawada indicative silver rates',asof.isoformat(),url=url)

def fuel():
 rows=list(csv.DictReader(io.StringIO(get('https://storage.data.gov.my/commodities/fuelprice.csv').text)))
 today=now.astimezone(MY_TZ).date().isoformat()
 valid=[r for r in rows if r.get('series_type')=='level' and r.get('date','')<=today]
 if not valid:raise ValueError('No effective fuel price records')
 v=max(valid,key=lambda r:r['date'])
 fields=('ron95_budi95','ron95','ron97','diesel')
 prices={k:float(v[k]) if v.get(k) else None for k in fields}
 if any(x is not None and not .5<x<10 for x in prices.values()):raise ValueError('Fuel price outside expected range')
 save('fuel',prices,'Malaysia government official fuel price schedule',v['date'],url='https://data.gov.my/data-catalogue/fuelprice')

for key,fn in [('sensex',sensex),('fx',exchange),('gold_my',malaysia_gold_goodreturns),('gold_in',india_gold),('silver_in',silver_india),('fuel',fuel)]:attempt(key,fn)
# Never substitute spot conversions for Goodreturns indicative gold rates. Stale observations keep original timestamps.
for key in ('sensex','fx','fuel','gold_my','gold_in','silver_in'):
 if key not in result['values'] and key in old.get('values',{}):
  entry=old['values'][key]
  if isinstance(entry,dict) and entry.get('asof') and not entry.get('stale'):
   result['values'][key]={**entry,'stale':True}
# Publish only when meaningful market data changes. Collection timestamps and
# transient scrape errors alone should not cause a new Pages deployment.
def comparable_values(data):
 values = data.get('values', {})
 return {
  key: {field: value for field, value in entry.items() if field != 'asof'}
  if isinstance(entry, dict) else entry
  for key, entry in values.items()
 }

if comparable_values(result) == comparable_values(old) and OUT.exists():
 print('Market values unchanged; keeping existing JSON and skipping deployment.')
else:
 OUT.write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
 print('Market values changed; updated market-data.json.')
print('Retrieved:', list(result['values']), 'Errors:', result['errors'])
