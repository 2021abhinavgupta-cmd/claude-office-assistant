import re
def _normalize_title(t):
    if not t: return ''
    t = t.replace('“', '"').replace('”', '"').replace('‘', "'").replace('’', "'")
    t = re.sub(r'^\[.*?\]\s*', '', t)
    return t.lower().strip()

raw = 'Travel Content 2'
local = '[Reel] Travel Content 2'
print(repr(_normalize_title(raw)))
print(repr(_normalize_title(local)))
print(_normalize_title(raw) == _normalize_title(local))

raw2 = '"Looking expensive is not the same as looking credible."'
local2 = '[Reel] “Looking expensive is not the same as looking credible.”'
print(repr(_normalize_title(raw2)))
print(repr(_normalize_title(local2)))
print(_normalize_title(raw2) == _normalize_title(local2))
