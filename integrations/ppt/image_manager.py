"""Image acquisition: project-local, Wikimedia Commons, or OpenAI image API."""
from pathlib import Path
from PIL import Image,ImageOps
import io,os,json,base64,urllib.parse,urllib.request,uuid,re
def _read(url,body=None,headers=None):
    req=urllib.request.Request(url,data=body,headers={"User-Agent":"RepoMedic-PPT/5.0",**(headers or {})})
    with urllib.request.urlopen(req,timeout=30) as response: data=response.read(12_000_001)
    if len(data)>12_000_000: raise ValueError("图片过大")
    return data
def _save(data,folder):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    with Image.open(io.BytesIO(data)) as im:
        if im.format not in ("JPEG","PNG","WEBP") or min(im.size)<180: raise ValueError("图片格式或分辨率不合适")
        im=ImageOps.exif_transpose(im).convert("RGB")
        dest=folder/(uuid.uuid4().hex+".png");im.save(dest)
    return str(dest)
def acquire_image(root,spec,folder):
    if not isinstance(spec,dict): raise ValueError("image 必须为对象")
    source=spec.get("source","auto"); factual=spec.get("factual",False)
    if source not in ("auto","local","web","ai"): raise ValueError("未知图片来源")
    if not isinstance(factual,bool): raise ValueError("factual 必须为布尔值")
    choices=(["local","web"] if factual else ["local","web","ai"]) if source=="auto" else [source]
    failures=[]
    for choice in choices:
        try:
            if choice=="local" and spec.get("path"):
                home=(Path(root)/"assets"/"images").resolve()
                candidate=(home/spec["path"]).resolve()
                if not candidate.is_relative_to(home) or not candidate.is_file(): raise ValueError("本地图片须在 assets/images 下")
                return {"path":_save(candidate.read_bytes(),folder),"source":"local","license":"user-provided"}
            if choice=="web" and spec.get("query"):
                params=urllib.parse.urlencode({"action":"query","generator":"search","gsrsearch":"filetype:bitmap "+spec["query"],"gsrnamespace":6,"gsrlimit":10,"prop":"imageinfo","iiprop":"url|extmetadata","iiurlwidth":1600,"format":"json"})
                pages=json.loads(_read("https://commons.wikimedia.org/w/api.php?"+params)).get("query",{}).get("pages",{})
                for page in pages.values():
                    for info in page.get("imageinfo",[]):
                        metadata=info.get("extmetadata",{})
                        license=metadata.get("LicenseShortName",{}).get("value","")
                        url=info.get("thumburl") or info.get("url","")
                        if not license or not url.startswith("https://upload.wikimedia.org/"): continue
                        try:
                            return {"path":_save(_read(url),folder),"source":"commons","license":re.sub("<[^>]*>","",license),"source_url":info.get("descriptionurl"),"author":re.sub("<[^>]*>","",metadata.get("Artist",{}).get("value",""))[:200]}
                        except (ValueError,OSError): continue
                raise ValueError("未找到合适的开放许可图片")
            if choice=="ai" and spec.get("prompt"):
                if factual: raise ValueError("禁止 AI 虚构真实科研结果")
                key=os.environ.get("OPENAI_API_KEY")
                if not key: raise ValueError("未设置 OPENAI_API_KEY")
                body=json.dumps({"model":"gpt-image-1","prompt":spec["prompt"],"size":"1536x1024","n":1}).encode()
                response=json.loads(_read("https://api.openai.com/v1/images/generations",body,{"Authorization":"Bearer "+key,"Content-Type":"application/json"}))
                return {"path":_save(base64.b64decode(response["data"][0]["b64_json"]),folder),"source":"ai","prompt":spec["prompt"],"license":"AI-generated"}
        except Exception as exc: failures.append(choice+": "+str(exc))
    raise ValueError("无法获取图片："+"；".join(failures or ["缺少 path/query/prompt"]))
