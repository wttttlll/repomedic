"""Rule-based content preparation for reference_fill.
Only emits layouts implemented by ppt_template_filler.
"""
def _short_label(text):
    text=str(text).strip()
    for sep in ("：", ":","，",",","。"):
        if sep in text and 2 <= len(text.split(sep)[0]) <= 14:
            return text.split(sep)[0]
    return text[:12].rstrip("。") or "研究要点"

def prepare_slide(item):
    if not isinstance(item, dict):
        raise ValueError("幻灯片必须为对象")
    result=dict(item)
    mode=result.get("layout", "auto")
    if mode in (None,"auto"):
        if result.get("table"):
            mode="table"
        elif result.get("blocks"):
            mode="info_blocks"
        elif result.get("left") or result.get("right"):
            mode="two_column"
        else:
            bullets=result.get("bullets") or []
            mode="info_blocks" if 2 <= len(bullets) <= 4 else "bullets"
        result["layout"]=mode
    if mode=="info_blocks" and not result.get("blocks"):
        bullets=result.get("bullets") or []
        if not bullets:
            raise ValueError("info_blocks 需要 blocks 或 bullets")
        result["blocks"]=[
            {"title":_short_label(txt), "body":str(txt)} for txt in bullets[:4]
        ]
    if mode=="table" and not result.get("table"):
        raise ValueError("table 页面需要 table.headers 和 table.rows")
    return result
