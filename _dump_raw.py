import json
d = json.load(open(r'C:\code\Count_and_Design\agent3-debug.json', encoding='utf-8'))
tr = d['table_recognition_results'][0]['data']
raw_llm = tr.get('raw_llm') or {}
raw = raw_llm.get('raw') or {}
print('choices:', json.dumps(raw.get('choices'), ensure_ascii=False, indent=1)[:1200])
print('---')
content = raw_llm.get('content', '')
print('content first 200:', content[:200])
print('content last 100:', content[-100:])
print('content startswith [:', content.lstrip().startswith('['))
print('fence count:', content.count(chr(96)*3))
# 模拟当前 parse_json_object 的 else 分支截取
s = content.strip()
start = s.find('{'); end = s.rfind('}')
cut = s[start:end+1] if start >= 0 and end > start else s
print('--- cut result preview ---')
print(cut[:150])
try:
    json.loads(cut)
    print('cut parses OK')
except json.JSONDecodeError as e:
    print('cut fails:', e)
# 去掉 fence 后顶层数组是否合法
import re
m = re.search(r'```(?:json)?\s*(.*?)\s*```', content, re.S)
inner = m.group(1) if m else content
try:
    arr = json.loads(inner)
    print('array parses OK, len:', len(arr), '| first keys:', list(arr[0].keys()))
except Exception as e:
    print('array parse fails:', e)