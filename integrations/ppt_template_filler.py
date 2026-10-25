"""Fill a fixed PPTX base with editable objects, using an existing deck as a style reference.

Scope: reusable content pages of base.pptx. Does not claim pixel-perfect design
inference or faithful table-style inference when no source table exists.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re
import uuid

from pptx import Presentation
from integrations.ppt_layout_engine import prepare_slide
from pptx.dml.color import RGBColor
from pptx.enum.text import MSO_AUTO_SIZE, PP_ALIGN, MSO_ANCHOR
from pptx.enum.dml import MSO_LINE_DASH_STYLE
from pptx.enum.shapes import MSO_SHAPE
from pptx.util import Inches, Pt

MAX_SLIDES = 30
ALLOWED_LAYOUTS = {"cover", "outline", "section", "bullets", "two_column",
                   "cards", "quote", "chart", "timeline", "table", "info_blocks"}


def _safe_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[\w\- ]{1,80}\.pptx", value, re.UNICODE):
        raise ValueError("filename 必须是安全的 .pptx 文件名")
    return value


def _hex_color(value, default):
    if not isinstance(value, str) or not re.fullmatch(r"#?[0-9A-Fa-f]{6}", value):
        value = default
    return RGBColor.from_string(value.lstrip("#"))


def _read_style(reference):
    """Read title typography AND geometry from reference content pages."""
    prs = Presentation(str(reference))
    from collections import Counter
    titles = []
    bodies = []
    for slide in prs.slides:
        for shape in slide.shapes:
            if not shape.has_text_frame or not shape.text.strip():
                continue
            for paragraph in shape.text_frame.paragraphs:
                for run in paragraph.runs:
                    if not run.text.strip():
                        continue
                    font = run.font
                    size = font.size.pt if font.size else None
                    try:
                        color = str(font.color.rgb) if font.color.rgb else None
                    except (AttributeError, TypeError, ValueError):
                        color = None
                    entry = {
                        "name": font.name,
                        "size": size,
                        "color": color,
                        "x": round(shape.left / Inches(1), 4),
                        "y": round(shape.top / Inches(1), 4),
                        "w": round(shape.width / Inches(1), 4),
                        "h": round(shape.height / Inches(1), 4),
                        "alignment": paragraph.alignment,
                    }
                    # Reference slides 3/4 have a 36pt centered page heading.
                    if size is not None and size >= 30 and entry["y"] < 0.9 and entry["w"] >= 4:
                        titles.append(entry)
                    elif size is not None and 12 <= size <= 24 and entry["y"] >= 0.7:
                        bodies.append(entry)
    if not titles:
        raise ValueError("参考 PPT 没找到顶部主标题，请检查 reference.pptx")
    title = Counter(
        (v["x"], v["y"], v["w"], v["h"], v["size"], v["name"], v["color"], v["alignment"])
        for v in titles
    ).most_common(1)[0][0]
    title_data = dict(zip(
        ("x","y","w","h","size","name","color","alignment"), title
    ))
    body_sizes = [v["size"] for v in bodies if v["size"]]
    body_fonts = [v["name"] for v in bodies if v["name"]]
    body_colors = [v["color"] for v in bodies if v["color"] and v["color"] != "FFFFFF"]
    body = {
        "size": Counter(body_sizes).most_common(1)[0][0] if body_sizes else 18,
        "name": Counter(body_fonts).most_common(1)[0][0] if body_fonts else "Times New Roman",
        "color": Counter(body_colors).most_common(1)[0][0] if body_colors else "243D67",
    }
    return {"title": title_data, "body": body}



def _add_text(slide, text, x, y, w, h, style, *, size=None, bold=False, align=None):
    shape = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = shape.text_frame
    tf.clear()
    tf.word_wrap = True
    tf.auto_size = MSO_AUTO_SIZE.NONE
    tf.margin_left = tf.margin_right = Inches(0.04)
    tf.margin_top = tf.margin_bottom = Inches(0.02)
    p = tf.paragraphs[0]
    p.text = str(text)
    if align is not None:
        p.alignment = align
    p.font.name = style["name"] or "Times New Roman"
    p.font.size = Pt(size or style["size"] or 18)
    p.font.bold = bold
    p.font.color.rgb = _hex_color(style["color"], "243D67")
    return shape



def _visual_units(text):
    return sum(1.0 if ord(ch) > 255 else .55 for ch in str(text))


def _table_geometry(spec, x, y, max_w, max_h):
    """Estimate column widths and row heights without reducing font size."""
    headers, rows = spec["headers"], spec["rows"]
    cols = len(headers)
    if cols > 8:
        raise ValueError("表格最多8列，请拆分")
    samples = [headers] + rows
    weights = []
    for j in range(cols):
        values = sorted((_visual_units(line[j]) for line in samples), reverse=True)
        weights.append(max(5, min(29, values[min(1, len(values)-1)] + 2)))
    total = sum(weights)
    widths = [max_w * v / total for v in weights]
    base_font = 14
    row_heights = []
    for row in samples:
        lines = max(
            max(1, __import__("math").ceil((_visual_units(value) * base_font / 90) / max(widths[j] - .16, .3)))
            for j, value in enumerate(row)
        )
        row_heights.append(max(.42, lines * .255 + .18))
    needed = sum(row_heights)
    if needed > max_h:
        raise ValueError(f"表格预计需要 {needed:.2f} 英寸，但页面只有 {max_h:.2f} 英寸；请减少行数或拆页")
    return x, y + (max_h-needed)/2, widths, row_heights


def _add_table(slide, spec, x, y, w, h, style):
    if not isinstance(spec, dict):
        raise ValueError("table 必须为对象")
    headers, rows = spec.get("headers"), spec.get("rows")
    if not isinstance(headers, list) or not headers or any(not isinstance(v, str) for v in headers):
        raise ValueError("table.headers 必须是非空字符串数组")
    if not isinstance(rows, list) or any(not isinstance(row, list) or len(row) != len(headers)
                                         or any(not isinstance(v, str) for v in row) for row in rows):
        raise ValueError("table.rows 必须是字符串二维数组，且每行列数一致")
    if len(rows) > 18:
        raise ValueError("单页最多18行数据；内容过多请拆页")
    x, y, col_widths, row_heights = _table_geometry(spec, x, y, w, h)
    shape = slide.shapes.add_table(len(rows)+1, len(headers), Inches(x), Inches(y),
                                   Inches(w), Inches(sum(row_heights)))
    table = shape.table
    table.first_row = False
    for j, cw in enumerate(col_widths):
        table.columns[j].width = Inches(cw)
    for i, rh in enumerate(row_heights):
        table.rows[i].height = Inches(rh)
    for i, line in enumerate([headers]+rows):
        for j, value in enumerate(line):
            cell = table.cell(i, j)
            cell.text = value
            cell.margin_left = cell.margin_right = Inches(.075)
            cell.margin_top = cell.margin_bottom = Inches(.035)
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            cell.fill.solid()
            cell.fill.fore_color.rgb = _hex_color(
                "4B7CC8" if i == 0 else ("FFFFFF" if i % 2 else "F0F5FA"), "FFFFFF"
            )
            for para in cell.text_frame.paragraphs:
                para.alignment = PP_ALIGN.CENTER if i == 0 else PP_ALIGN.LEFT
                for run in para.runs:
                    run.font.name = "Microsoft YaHei" if any(ord(c)>255 for c in value) else (style["name"] or "Times New Roman")
                    run.font.size = Pt(14)
                    run.font.bold = i == 0
                    run.font.color.rgb = _hex_color("FFFFFF" if i==0 else "17243A", "17243A")
    return shape


def _add_info_blocks(slide, blocks, width, top, height, style):
    if not isinstance(blocks, list) or not 1 <= len(blocks) <= 4:
        raise ValueError("info_blocks 需要 1～4 个模块")
    if any(not isinstance(b,dict) or not isinstance(b.get("title"), str) for b in blocks):
        raise ValueError("每个模块必须提供 title 字符串")
    import math
    ncols = 2 if len(blocks) in (2,4) else 1
    nrows = math.ceil(len(blocks) / ncols)
    gap_x, gap_y = .35, .35
    left = .55
    panel_w = (width-2*left-(ncols-1)*gap_x)/ncols
    panel_h = (height-(nrows-1)*gap_y)/nrows
    for i,b in enumerate(blocks):
        row, col = divmod(i,ncols)
        x=left + col*(panel_w+gap_x)
        y=top + row*(panel_h+gap_y)
        if panel_h < 1.25:
            raise ValueError("模块高度不足，请减少模块数量")
        frame=slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(x), Inches(y+.18),
                                      Inches(panel_w), Inches(panel_h-.23))
        frame.fill.background()
        frame.line.color.rgb=_hex_color("15253A","15253A")
        frame.line.width=Pt(1.6)
        frame.line.dash_style=MSO_LINE_DASH_STYLE.DASH
        label_width=min(panel_w-.35, max(1.65, _visual_units(b["title"])*.115+.5))
        label=slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(x+.22), Inches(y),
                                     Inches(label_width), Inches(.42))
        label.fill.solid()
        label.fill.fore_color.rgb=_hex_color("4B7CC8","4B7CC8")
        label.line.fill.background()
        _add_text(slide,b["title"],x+.28,y+.035,label_width-.12,.34,
                  {"name":"Microsoft YaHei","size":17,"color":"FFFFFF"},bold=True)
        body=b.get("body", b.get("bullets", []))
        if isinstance(body,list):
            body="\n".join("• "+str(v) for v in body)
        if not isinstance(body,str):
            raise ValueError("模块正文必须是字符串或列表")
        if _visual_units(body)>panel_w*panel_h*27:
            raise ValueError(f"模块 {i+1} 的内容过长，请拆页")
        _add_text(slide,body,x+.24,y+.55,panel_w-.48,panel_h-.77,
                  {"name":"Microsoft YaHei","size":16,"color":"17243A"})


def _clone_slide(prs, source):
    """Clone page shapes and their relationships (including images) within the same PPTX."""
    target = prs.slides.add_slide(source.slide_layout)
    for shape in list(target.shapes):
        shape._element.getparent().remove(shape._element)

    rel_map = {}
    for rel in source.part.rels.values():
        if rel.reltype.endswith("/slideLayout") or rel.reltype.endswith("/notesSlide"):
            continue
        if rel.is_external:
            new_id = target.part.relate_to(rel.target_ref, rel.reltype, is_external=True)
        else:
            new_id = target.part.relate_to(rel.target_part, rel.reltype)
        rel_map[rel.rId] = new_id

    for shape in source.shapes:
        cloned = deepcopy(shape.element)
        for el in cloned.iter():
            for attr, value in list(el.attrib.items()):
                if attr.startswith("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}") and value in rel_map:
                    el.set(attr, rel_map[value])
        target.shapes._spTree.insert_element_before(cloned, "p:extLst")
    return target


def _remove_slide(prs, idx):
    slide_id = prs.slides._sldIdLst[idx]
    prs.part.drop_rel(slide_id.rId)
    prs.slides._sldIdLst.remove(slide_id)


def _fill(slide, item, style, width, height, warnings, index):
    if not isinstance(item, dict):
        raise ValueError("slides 的每一项必须是对象")
    kind = item.get("layout", "bullets")
    if kind not in ALLOWED_LAYOUTS:
        raise ValueError(f"不支持的布局: {kind}")
    title = str(item.get("title") or "未命名页面")
    if len(title) > 120:
        raise ValueError("标题超过 120 字")
    # Preserve the original base artwork; write only inside its content region.
    title_style = style["title"]
    # The reference title is white on a navy top band; draw the band on the
    # cloned blank template so white lettering remains legible.
    from pptx.enum.shapes import MSO_SHAPE
    band = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE, Inches(0), Inches(0),
        Inches(width), Inches(max(0.78, title_style["y"] + title_style["h"] + 0.04))
    )
    band.fill.solid()
    band.fill.fore_color.rgb = _hex_color("061E55", "061E55")
    band.line.fill.background()
    title_style = {**title_style, "color": "FFFFFF"}
    _add_text(
        slide, title,
        title_style["x"], title_style["y"],
        title_style["w"], title_style["h"],
        title_style, bold=True,
        align=title_style.get("alignment") or PP_ALIGN.CENTER,
    )
    top = 1.6
    available_h = max(1.2, height - top - .7)
    if kind == "table" and "table" not in item:
        raise ValueError(f"第 {index} 页 layout=table，但未传入 table.headers 和 table.rows；禁止生成占位文字")
    if kind == "table" or "table" in item:
        _add_table(slide, item.get("table") or {}, .92, top,
                   width - 1.84, min(available_h, 4.6), style["body"])
    elif kind == "info_blocks":
        _add_info_blocks(slide, item.get("blocks"), width, top, available_h, style["body"])
    elif kind == "two_column":
        for x, key in ((.95, "left"), (width / 2 + .15, "right")):
            contents = item.get(key) or []
            if not isinstance(contents, list):
                raise ValueError(f"{key} 必须是数组")
            _add_text(slide, "\n\n".join(str(v) for v in contents[:6]),
                      x, top, width / 2 - 1.2, available_h,
                      style["body"], size=min(style["body"]["size"] or 18, 19))
    else:
        content = item.get("bullets") or []
        if not isinstance(content, list):
            raise ValueError("bullets 必须是数组")
        if kind == "outline":
            content = [f"{i + 1}. {s}" for i, s in enumerate(content)]
        elif kind == "quote":
            content = [item.get("quote", "")]
        elif kind == "timeline":
            content = [f'{e.get("label", "")}: {e.get("text", "")}'
                       for e in item.get("events", []) if isinstance(e, dict)]
        elif kind == "cards":
            content = [f'{e.get("title", "")}: {e.get("body", "")}'
                       for e in item.get("cards", []) if isinstance(e, dict)]
        if kind in ("cover", "section") and item.get("subtitle"):
            content = [str(item["subtitle"])] + content
        lines = [str(v) for v in content]
        if kind == "chart":
            raise ValueError("reference_fill 暂不支持 chart，请使用图表专用模式")
        if len(lines) > 9 or sum(len(s) for s in lines) > 700:
            raise ValueError(f"第 {index} 页内容过多，请拆页，避免破坏固定字号")
        if kind == "bullets" and lines:
            from math import ceil
            n = len(lines)
            gap = .18
            box_h = (available_h - gap*(n-1))/n
            if box_h < .62:
                raise ValueError("正文过多，请拆页")
            for j, line in enumerate(lines):
                y = top + j*(box_h+gap)
                label = slide.shapes.add_shape(
                    MSO_SHAPE.RECTANGLE, Inches(.75), Inches(y+.05),
                    Inches(.075), Inches(min(.4,box_h-.1)))
                label.fill.solid()
                label.fill.fore_color.rgb = _hex_color("4B7CC8","4B7CC8")
                label.line.fill.background()
                _add_text(slide, line, .95, y, width-1.85, box_h,
                          style["body"],size=18)
        else:
            content_text = "\n\n".join(lines)
            _add_text(slide, content_text, 1.0, top,
                      width - 2.0, available_h, style["body"])
        if kind == "cover":
            warnings.append("底板无独立封面页：cover 使用空白内容页，非原模板封面设计")
        if kind == "timeline":
            warnings.append(f"第 {index} 页时间线暂以可编辑文字呈现")


def generate_from_reference(*, base, filename, slides,
                            template_name="base.pptx",
                            reference_name="reference.pptx",
                            style_name="style_rules.json",
                            preview=True):
    base = Path(base).resolve()
    filename = _safe_name(filename)
    if not isinstance(slides, list) or not 1 <= len(slides) <= MAX_SLIDES:
        raise ValueError("slides 必须包含 1～30 页")
    folder = base / "templates" / "ppt"
    template = folder / _safe_name(template_name)
    reference = folder / _safe_name(reference_name)
    if not template.is_file() or not reference.is_file():
        raise FileNotFoundError("缺少 templates/ppt/base.pptx 或 reference.pptx")
    if any(f.is_symlink() or f.stat().st_size > 30 * 1024 * 1024 for f in (template, reference)):
        raise ValueError("模板链接或大小不符合要求")
    prs = Presentation(str(template))
    if len(prs.slides) < 5:
        raise ValueError("此版本要求 base.pptx 至少有 5 页，内容页位于第 3/5 页")
    style = _read_style(reference)
    # style_name is reserved for the advanced JSON style-inference phase.
    warnings = ["标题位置、字号、颜色和对齐来自参考 PPT；表格与信息模块为新建的自适应设计规则",
                "表格默认样式为临时规范，参考 PPT 未提供可复用的原生表格样例"]
    if preview:
        warnings.append("此填充模式尚未生成预览，请使用 PowerPoint 检查")
    width = prs.slide_width / Inches(1)
    height = prs.slide_height / Inches(1)

    # Create all output pages before removing original pages, preserving source objects.
    original_count = len(prs.slides)
    for i, original_item in enumerate(slides, 1):
        item = prepare_slide(original_item)
        layout_type = item.get("layout", "bullets") if isinstance(item, dict) else "bullets"
        source = prs.slides[4 if layout_type == "table" or (isinstance(item, dict) and "table" in item) else 2]
        generated = _clone_slide(prs, source)
        _fill(generated, item, style, width, height, warnings, i)

    for _ in range(original_count):
        _remove_slide(prs, 0)

    output = base / "output" / "ppt"
    output.mkdir(parents=True, exist_ok=True)
    destination = output / filename
    if destination.is_symlink():
        raise ValueError("输出路径禁止为符号链接")
    staging = output / ("." + uuid.uuid4().hex + ".pptx")
    try:
        prs.save(str(staging))
        staging.replace(destination)
    finally:
        staging.unlink(missing_ok=True)

    return {
        "success": True,
        "pptx": str(destination),
        "slides_created": len(slides),
        "total_slides": len(prs.slides),
        "template": template_name,
        "reference": reference_name,
        "warnings": warnings,
        "preview": [],
    }
