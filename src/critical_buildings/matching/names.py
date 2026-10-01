"""Name matching shared by the merge, site and register-matching stages."""
import re
from difflib import SequenceMatcher

from critical_buildings import config


def _tokens(name):
    if name is None or (isinstance(name, float) and name != name):      # None / NaN: no name, no tokens
        return []
    s = re.sub(r"[^a-z0-9 ]", " ", str(name).lower().replace("&", " and ").replace("st.", "saint").replace("st ", "saint "))
    toks = [t for t in s.split() if t]
    keep = [t for t in toks if t not in config.get().reference_match.stop_words]
    return keep or toks


def strict_similarity(a, b):
    """Full-name similarity (no stop-word removal): 'Nayland Surgery' and 'Nayland House Care Home' are different."""
    na = re.sub(r"[^a-z0-9]", "", str(a).lower().replace("&", "and"))
    nb = re.sub(r"[^a-z0-9]", "", str(b).lower().replace("&", "and"))
    return SequenceMatcher(None, na, nb).ratio() if na and nb else 0.0


def name_similarity(a, b):
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    sa, sb = set(ta), set(tb)
    contain = len(sa & sb) / min(len(sa), len(sb))
    jacc = len(sa & sb) / len(sa | sb)
    seq = SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio()
    return max(contain * 0.9, jacc, seq)
