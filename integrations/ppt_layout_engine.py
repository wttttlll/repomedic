def choose_layout(item):
    """根据内容结构选择布局。"""
    if item.get("table"):
        return "table"

    if item.get("image"):
        return "image_text"

    if item.get("left") and item.get("right"):
        return "two_column"

    if item.get("blocks"):
        return "info_blocks"

    bullets = item.get("bullets", [])

    if len(bullets) in (2, 3, 4):
        return "info_blocks"

    if len(bullets) >= 5:
        return "structured_list"

    return "bullets"


def prepare_slide(item):
    result = dict(item)

    # 只有未明确指定布局时才自动选择
    if result.get("layout") in (None, "auto"):
        result["layout"] = choose_layout(result)

    if result["layout"] == "info_blocks" and not result.get("blocks"):
        result["blocks"] = [
            {
                "title": f"要点 {i + 1}",
                "body": text,
            }
            for i, text in enumerate(result.get("bullets", []))
        ]

    return result