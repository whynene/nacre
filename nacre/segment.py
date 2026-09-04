import re

import jieba

_TOKEN = re.compile(r"[\w一-鿿]+")

_STOPWORDS = set("的了着是在就都很也又吗呢吧啊呀哦嘛么和与这那个") | {"这个", "那个", "什么", "怎么", "还是", "就是", "但是", "然后"}

def tokens(text):
    out = []
    for tok in jieba.lcut(text or ""):
        tok = tok.strip()
        if tok and tok not in _STOPWORDS and _TOKEN.fullmatch(tok):
            out.append(tok.lower())
    return out

def seg_text(text):
    return " ".join(tokens(text))

_ASCII_NAME = re.compile(r"^[\x00-\x7f]+$")

def mentions(text, names):
    t = (text or "")
    low = t.lower()
    hit = []
    for name in names:
        n = (name or "").strip()
        if not n:
            continue
        if _ASCII_NAME.match(n):
            if re.search(r"(?<![0-9A-Za-z_])%s(?![0-9A-Za-z_])" % re.escape(n), t, re.I):
                hit.append(n)
        elif n.lower() in low:
            hit.append(n)
    kept = [n for n in hit if not any(n != o and n.lower() in o.lower() for o in hit)]
    seen, out = set(), []
    for n in kept:
        if n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return out

def name_phrase(name):
    seg = seg_text(name)
    if not seg:
        return None
    return '"%s"' % seg

def entity_fts_query(names):
    parts = [p for p in (name_phrase(n) for n in names) if p]
    if not parts:
        return None
    return " OR ".join(parts)
