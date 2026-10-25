from pathlib import Path
import ast
import shutil

BASE = Path(__file__).resolve().parent

def patch_definitions():
    path = BASE / "tools" / "definitions.py"
    original = path.read_text(encoding="utf-8")
    if "_configure_reference_fill_v4()" in original:
        print("definitions.py already patched")
        return
    extra = '''
def _configure_reference_fill_v4():
    entry = next(t["function"] for t in TOOLS
                 if t.get("type") == "function"
                 and t["function"].get("name") == "generate_ppt")
    props = entry["parameters"]["properties"]
    props.setdefault("template_mode", {"type":"string"})["enum"] = [
        "replace", "append", "reference_fill"]
    props.setdefault("palette", {"type":"string"})["enum"] = [
        "ocean", "forest", "sunset", "minimal", "academic_navy"]
    slide_props = props["slides"]["items"]["properties"]
    slide_props.setdefault("layout", {"type":"string"})["enum"] = [
        "auto", "cover", "outline", "section", "bullets",
        "two_column", "cards", "quote", "chart", "timeline", "table", "info_blocks"]
    slide_props["table"] = {
        "type":"object",
        "properties":{
            "headers":{"type":"array","items":{"type":"string"}},
            "rows":{"type":"array","items":{
                "type":"array","items":{"type":"string"}}}
        },"required":["headers","rows"],"additionalProperties":False}
    slide_props["blocks"] = {
        "type":"array","minItems":1,"maxItems":4,
        "items":{"type":"object","properties":{
            "title":{"type":"string"},
            "body":{"type":"string"},
            "bullets":{"type":"array","items":{"type":"string"}}
        },"required":["title"],"additionalProperties":False}}

_configure_reference_fill_v4()
'''
    revised = original.rstrip() + "\n\n" + extra
    ast.parse(revised)
    backup = path.with_suffix(".py.before_ppt_v4.bak")
    if not backup.exists():
        shutil.copy2(path, backup)
    path.write_text(revised, encoding="utf-8")
    print("Patched definitions.py")

if __name__ == "__main__":
    patch_definitions()
    print("Keep your existing working local_tools.py and ppt_generator.py")
