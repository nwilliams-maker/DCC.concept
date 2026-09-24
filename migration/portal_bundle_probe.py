from __future__ import annotations

import json
import re
import requests
from urllib.parse import urljoin

BASE="https://manage.terraboost.com/workorders?pageSize=50"

def main():
    r=requests.get(BASE,timeout=30)
    r.raise_for_status()
    scripts=re.findall(r'<script[^>]+src=["\']([^"\']+)["\']',r.text,flags=re.I)
    out={"page_status":r.status_code,"scripts":[]}
    needles=[
        "query GetWorkOrderById",
        "taskType {",
        "typeGroups:",
        "query GetRecord",
        "recordTypes(",
        "const J0t",
        "J0t=Kt",
    ]
    for src in scripts:
        u=urljoin(r.url,src)
        try:
            js=requests.get(u,timeout=40).text
        except Exception as exc:
            out["scripts"].append({"url":u,"error":str(exc)})
            continue
        matches=[]
        for needle in needles:
            start=0
            found=0
            while True:
                pos=js.find(needle,start)
                if pos<0 or found>=12:
                    break
                matches.append({
                    "needle":needle,
                    "pos":pos,
                    "snippet":js[max(0,pos-1000):pos+7000]
                })
                start=pos+len(needle)
                found+=1
        out["scripts"].append({"url":u,"size":len(js),"matches":matches[:40]})
    print("BUNDLE_PROBE="+json.dumps(out,separators=(",",":")))

if __name__=="__main__":
    main()
