"""Summarise a dedup plan: which side is removed, name/size/date agreement, evidence.

    python -I out/plan_review.py <plan.csv>
"""
import collections
import csv
import os
import re
import sys

STAGE = r"e:\kids photos\stage\julia-and-elizabeth"
rows = list(csv.DictReader(open(sys.argv[1], encoding="utf-8")))
groups = collections.defaultdict(list)
for r in rows:
    groups[r["group"]].append(r)


def side(r):
    s = r["sources"].lower()
    return "album" if STAGE in s else "other"


def stem(r):
    b = os.path.splitext(os.path.basename(r["path"]))[0]
    b = re.sub(r"^\d{4}-\d\d-\d\d_\d{6}_", "", b)
    return re.sub(r"_[0-9a-f]{8}$", "", b).lower()


C = collections.Counter()
flags = collections.Counter()
ev = []
exts = collections.Counter()
newside = collections.Counter()
for g, rs in groups.items():
    keep = [r for r in rs if r["action"] == "keep"]
    rem = [r for r in rs if r["action"] != "keep"]
    k = keep[0]
    for r in rem:
        C[(r["class"], "removed=" + side(r), "kept=" + side(k))] += 1
        flags["stem same"] += stem(r) == stem(k)
        flags["same size"] += (r["w"], r["h"]) == (k["w"], k["h"])
        flags["same date"] += r["date"] == k["date"]
        flags["removed has live/motion"] += r["live_or_motion"] == "True" or bool(r["live_clips"])
        flags["kept has live/motion"] += k["live_or_motion"] == "True" or bool(k["live_clips"])
        exts[(os.path.splitext(r["path"])[1].lower(), os.path.splitext(k["path"])[1].lower())] += 1
        ev.append(r["evidence"])
        newside[r["path"].split("/")[0] + "/" + r["path"].split("/")[1]] += 1
print("groups", len(groups), "removals", sum(C.values()))
for k, v in C.most_common():
    print("  ", k, v)
print("flags", dict(flags))
print("ext removed->kept", dict(exts))
print("removed from volumes", dict(newside))
print("evidence samples", collections.Counter(re.sub(r"[0-9.]+", "#", e) for e in ev).most_common(5))
nums = [float(m) for e in ev for m in re.findall(r"mad[= ]([0-9.]+)", e)]
if nums:
    print("mad: zero", sum(n == 0 for n in nums), "max", max(nums))
