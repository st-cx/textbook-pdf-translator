"""Value-based numeric checks for bilingual textbook dates and centuries."""
from collections import Counter
import re


def numeric_fingerprint(text):
    ordinals=['first','second','third','fourth','fifth','sixth','seventh','eighth','ninth','tenth',
              'eleventh','twelfth','thirteenth','fourteenth','fifteenth','sixteenth','seventeenth',
              'eighteenth','nineteenth','twentieth','twenty-first']
    months=['January','February','March','April','May','June','July','August','September','October','November','December']
    values=[]
    def capture(kind,value):
        values.append((kind,int(value)))
        return ' '
    for n,word in enumerate(ordinals,1):
        text=re.sub(r'\b'+word+r'[-\s]+centur(?:y|ies)\b',lambda m,n=n:capture('century',n),text,flags=re.I)
    text=re.sub(r'\b(\d{1,2})(?:st|nd|rd|th)[-\s]+centur(?:y|ies)\b',lambda m:capture('century',m[1]),text,flags=re.I)
    text=re.sub(r'(?<![\dA-Za-z])(\d{2})00s\b',lambda m:capture('century',int(m[1])+1),text)
    cn={'零':0,'〇':0,'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9}
    def chinese(value):
        if value.isdigit():return int(value)
        if '十' in value:
            a,b=value.split('十',1)
            return (cn.get(a,1) if a else 1)*10+(cn.get(b,0) if b else 0)
        return cn.get(value,0)
    text=re.sub(r'([0-9]{1,2}|[一二三四五六七八九十两]{1,3})世纪',lambda m:capture('century',chinese(m[1])),text)
    for n,word in enumerate(months,1):
        # A date numeral is required to avoid the English verbs may/march.
        text=re.sub(r'\b'+word+r'\b(?=\s+\d)',lambda m,n=n:capture('month',n),text)
    text=re.sub(r'([0-9]{1,2}|[一二三四五六七八九十两]{1,3})月',lambda m:capture('month',chinese(m[1])),text)
    values.extend(('number',v) for v in re.findall(r'(?<![A-Za-z0-9_])\d+(?:\.\d+)?',text))
    return Counter(values)
