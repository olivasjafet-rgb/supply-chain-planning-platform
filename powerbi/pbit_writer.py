"""Write a Power BI template (.pbit) directly, without pbi-tools.

pbi-tools 1.2 binds against an older Power BI Desktop packaging assembly and
fails on Desktop 2.157 with a MissingMethodException. A .pbit is just an OPC
zip with a handful of parts, so we write it ourselves and skip the dependency
entirely.

Parts of a .pbit:
    [Content_Types].xml   UTF-8 XML, declares the part extensions
    Version               UTF-16LE, the template format version
    DataModelSchema       UTF-16LE, the TMSL model definition
    Report/Layout         UTF-16LE, the report definition
    Metadata, Settings    UTF-16LE, small JSON blobs
    SecurityBindings      UTF-16LE, empty binding set

The one detail that trips people up: inside Report/Layout, every `config` and
`filters` value is itself a JSON **string**, not a nested object. Emitting them
as objects produces a file Power BI opens with an empty canvas and no error.
"""

from __future__ import annotations

import codecs
import json
import zipfile
from pathlib import Path

BOM = codecs.BOM_UTF16_LE

CONTENT_TYPES = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="json" ContentType="" />'
    '<Override PartName="/Version" ContentType="" />'
    '<Override PartName="/DataModelSchema" ContentType="" />'
    '<Override PartName="/Report/Layout" ContentType="" />'
    '<Override PartName="/Metadata" ContentType="" />'
    '<Override PartName="/Settings" ContentType="" />'
    '<Override PartName="/SecurityBindings" ContentType="" />'
    '</Types>'
)


def _u16(text: str) -> bytes:
    return BOM + text.encode("utf-16-le")


def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def build_layout(pages: list[tuple[str, list[dict]]],
                 width: int = 1280, height: int = 920) -> dict:
    """Pack pages into the Report/Layout shape Power BI expects."""
    sections = []
    for i, (display, visuals) in enumerate(pages):
        containers = []
        for v in visuals:
            pos = v["layouts"][0]["position"]
            containers.append({
                "x": pos["x"], "y": pos["y"], "z": pos["z"],
                "width": pos["width"], "height": pos["height"],
                "tabOrder": pos.get("tabOrder", pos["z"]),
                "config": _j(v),          # <- JSON string, not an object
                "filters": "[]",
            })
        sections.append({
            "id": i,
            "name": f"ReportSection{i:04d}",
            "displayName": display,
            "ordinal": i,
            "visualContainers": containers,
            "config": "{}",
            "filters": "[]",
            "width": width,
            "height": height,
            "displayOption": 1,
        })

    return {
        "id": 0,
        "resourcePackages": [{
            "resourcePackage": {
                "name": "SharedResources",
                "type": 2,
                "items": [{"name": "CY24SU10", "path": "BaseThemes/CY24SU10.json",
                           "type": 202}],
                "disabled": False,
            }
        }],
        "sections": sections,
        "config": _j({
            "version": "5.43",
            "themeCollection": {"baseTheme": {"name": "CY24SU10", "version": "5.43",
                                              "type": 2}},
            "activeSectionIndex": 0,
            "defaultDrillFilterOtherVisuals": True,
            "settings": {"useStylableVisualContainerHeader": True},
        }),
        "layoutOptimization": 0,
        "publicCustomVisuals": [],
    }


def write_pbit(path: Path, model: dict, layout: dict,
               report_name: str = "Replenishment Control Tower") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()

    metadata = {
        "Version": 3,
        "AutoCreatedRelationships": [],
        "FileDescription": report_name,
        "CreatedFrom": "Desktop",
    }
    settings = {
        "Version": 4,
        "ReportSettings": {"UserConsentsToCompositeModels": True},
        "QueriesSettings": {"Version": 5,
                            "TypeDetectionEnabled": False,
                            "RelationshipImportEnabled": False},
    }
    security_bindings = {"Version": 1, "Bindings": []}

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        # Order matters to some readers: Version first, content types last.
        z.writestr("Version", _u16("3.0"))
        z.writestr("DataModelSchema", _u16(_j(model)))
        z.writestr("Report/Layout", _u16(_j(layout)))
        z.writestr("Metadata", _u16(_j(metadata)))
        z.writestr("Settings", _u16(_j(settings)))
        z.writestr("SecurityBindings", _u16(_j(security_bindings)))
        z.writestr("[Content_Types].xml", CONTENT_TYPES.encode("utf-8"))
    return path


def verify(path: Path) -> dict:
    """Re-open the file and confirm every part parses back."""
    report = {}
    with zipfile.ZipFile(path) as z:
        report["parts"] = z.namelist()
        for part in ("DataModelSchema", "Report/Layout", "Metadata", "Settings"):
            raw = z.read(part)
            assert raw.startswith(BOM), f"{part} is missing the UTF-16LE BOM"
            obj = json.loads(raw[len(BOM):].decode("utf-16-le"))
            report[part] = obj
    m = report["DataModelSchema"]["model"]
    lay = report["Report/Layout"]
    report["summary"] = {
        "tables": [t["name"] for t in m["tables"]],
        "measures": sum(len(t.get("measures", [])) for t in m["tables"]),
        "relationships": len(m.get("relationships", [])),
        "pages": [(s["displayName"], len(s["visualContainers"]))
                  for s in lay["sections"]],
    }
    # every container config must be a *string* holding valid JSON
    for s in lay["sections"]:
        for c in s["visualContainers"]:
            assert isinstance(c["config"], str), "visual config must be a JSON string"
            json.loads(c["config"])
    return report
