"""Static checks on the mini program source.

    python test_miniprogram.py

There is no headless runtime for WeChat mini programs, so this can't execute the
UI. What it can do is catch the failures that cost the most time in the IDE:
malformed JSON, a page listed in app.json without files on disk, a WXML binding
with no matching handler, and — the big one — the frontend reading a field the
API never returns.

That last check is why this file exists. It parses api.py for the keys each
endpoint returns and compares them against what the JS actually reads.
"""

import json
import pathlib
import re
import sys

MP = pathlib.Path("miniprogram")
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  -> ' + detail}")


def read(p):
    return (MP / p).read_text(encoding="utf-8")


# ---------------------------------------------------------------- structure
def test_json_valid():
    print("\nJSON files parse")
    for f in sorted(MP.rglob("*.json")):
        try:
            json.loads(f.read_text(encoding="utf-8"))
            ok, detail = True, ""
        except json.JSONDecodeError as e:
            ok, detail = False, str(e)
        check(f"{f.relative_to(MP)}", ok, detail)


def test_pages_exist():
    print("\npages declared in app.json exist")
    app = json.loads(read("app.json"))
    for page in app["pages"]:
        for ext in ("js", "wxml", "wxss", "json"):
            f = MP / f"{page}.{ext}"
            if ext in ("wxss", "json") and not f.exists():
                continue  # both are optional per page
            check(f"{page}.{ext}", f.exists(), "missing")
    check("digest page is the entry (first in list)",
          app["pages"][0] == "pages/digest/index", app["pages"][0])
    check("sitemap is referenced and present",
          (MP / app.get("sitemapLocation", "sitemap.json")).exists())


def test_navigation_targets():
    print("\nnavigation targets are declared pages")
    declared = {"/" + p for p in json.loads(read("app.json"))["pages"]}
    bad = []
    for f in MP.rglob("*.js"):
        for m in re.finditer(r"url:\s*['\"](/[^'\"?]+)", f.read_text(encoding="utf-8")):
            if m.group(1) not in declared:
                bad.append(f"{f.relative_to(MP)} -> {m.group(1)}")
    check("every navigate/reLaunch target is a declared page", not bad, str(bad))


def test_handlers_exist():
    print("\nWXML bindings have handlers")
    for wxml in sorted(MP.rglob("*.wxml")):
        js = wxml.with_suffix(".js")
        if not js.exists():
            continue
        src = js.read_text(encoding="utf-8")
        bound = set(re.findall(r"bind(?:tap|input|change|confirm):?\s*=\s*[\"'](\w+)",
                               wxml.read_text(encoding="utf-8")))
        missing = [h for h in bound
                   if not re.search(rf"^\s*(async\s+)?{h}\s*\(", src, re.M)]
        check(f"{wxml.relative_to(MP)} ({len(bound)} bindings)",
              not missing, f"no handler: {missing}")


def test_wxml_no_html_tags():
    print("\nWXML uses mini-program tags, not HTML")
    html_only = re.compile(r"<(div|span|p|a|ul|li|img|h[1-6])[\s>]")
    for wxml in sorted(MP.rglob("*.wxml")):
        found = set(html_only.findall(wxml.read_text(encoding="utf-8")))
        check(f"{wxml.relative_to(MP)}", not found, f"HTML tags: {found}")


# ---------------------------------------------------------------- contract
def test_api_contract():
    """The frontend must only read fields api.py actually returns."""
    print("\nfrontend/backend field contract")
    apisrc = pathlib.Path("api.py").read_text(encoding="utf-8")

    # every path the JS calls must exist as a route in api.py
    routes = set(re.findall(r"@app\.(?:get|post)\(\"([^\"]+)\"\)", apisrc))
    called = set(re.findall(r"call\('([^'?]+)'", read("utils/api.js")))
    unknown = called - routes
    check(f"all {len(called)} called paths exist in api.py", not unknown, str(unknown))

    # "/" and "/health" are for humans and the platform's probe, not the client
    unused = routes - called - {"/health", "/"}
    check("no route is silently unreachable from the client",
          not unused, f"unused: {unused}")

    js = " ".join((MP / p).read_text(encoding="utf-8")
                  for p in ["app.js", "utils/api.js"]) + " " + \
        " ".join(f.read_text(encoding="utf-8") for f in MP.rglob("pages/**/*.js"))
    wxml = " ".join(f.read_text(encoding="utf-8") for f in MP.rglob("*.wxml"))
    front = js + " " + wxml

    # /api/session -> service.open_session
    for field in ["new_user", "topics", "max_topics"]:
        check(f"session.{field} is produced and consumed",
              field in pathlib.Path("service.py").read_text(encoding="utf-8")
              and field in front)

    # /api/catalog -> Catalog.list_for_picker
    store = pathlib.Path("store.py").read_text(encoding="utf-8")
    for field in ["groups", "max_topics"]:
        check(f"catalog.{field} reaches the picker", field in front)
    for field in ["key", "label", "blurb"]:
        check(f"picker entry .{field} is produced by list_for_picker",
              f'"{field}"' in store)

    # /api/topics/custom -> service.create_custom_topic
    svc = pathlib.Path("service.py").read_text(encoding="utf-8")
    for field in ["key", "label", "focus", "preview"]:
        check(f"custom topic .{field} is produced and consumed",
              f'"{field}"' in svc and f"topic.{field}" in front)

    # /api/digest -> service.assemble -> render shape
    for field in ["sections", "headline", "date", "title"]:
        check(f"digest.{field} is produced and consumed",
              f'"{field}"' in svc and field in front)
    for field in ["kind", "items", "rows"]:
        check(f"section.{field} is handled by the template", field in wxml)


def test_section_kinds_covered():
    print("\nall section kinds render")
    wxml = read("pages/digest/index.wxml")
    svc = pathlib.Path("service.py").read_text(encoding="utf-8")
    kinds = set(re.findall(r'"kind":\s*"(\w+)"', svc)) | \
        set(re.findall(r'kind = "(\w+)"', svc))
    kinds |= {"tickers", "countdown", "items"}
    for k in sorted(kinds):
        check(f"kind '{k}' has a branch",
              f"'{k}'" in wxml or k == "items" and "wx:else" in wxml)


def test_config_is_fillable():
    """Config must be either an obvious placeholder or a real, valid value —
    never something in between that fails silently at runtime."""
    print("\nconfig values")
    cfg = read("utils/config.js")
    env = re.search(r"CLOUD_ENV:\s*'([^']*)'", cfg).group(1)
    check("CLOUD_ENV is a placeholder or a real env id",
          "xxxx" in env or re.fullmatch(r"[a-z]+-[0-9a-z]{6,}", env), env)
    check("DEV_BASE_URL is empty (production path)",
          re.search(r"DEV_BASE_URL:\s*''", cfg) is not None,
          "still pointing at a local server — clear it before publishing")
    check("SERVICE_NAME is set",
          bool(re.search(r"SERVICE_NAME:\s*'\S+'", cfg)))

    appid = re.search(r'"appid":\s*"([^"]*)"', read("project.config.json")).group(1)
    check("appid is a placeholder or a real wx appid",
          "YOUR_APPID" in appid or re.fullmatch(r"wx[0-9a-f]{16}", appid), appid)


def test_external_links_not_opened():
    print("\nexternal links are copied, not opened")
    js = read("pages/digest/index.js")
    check("uses clipboard for source URLs", "setClipboardData" in js)
    wxml = " ".join(f.read_text(encoding="utf-8") for f in MP.rglob("*.wxml"))
    check("no web-view (would need a filed business domain)",
          "<web-view" not in wxml)
    check("the UI tells the user what tapping does", "复制" in read(
        "pages/digest/index.wxml"))


if __name__ == "__main__":
    if not MP.exists():
        sys.exit("miniprogram/ not found — run from the repo root")
    print("open-digest mini program checks")
    test_json_valid()
    test_pages_exist()
    test_navigation_targets()
    test_handlers_exist()
    test_wxml_no_html_tags()
    test_api_contract()
    test_section_kinds_covered()
    test_config_is_fillable()
    test_external_links_not_opened()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("failed: " + ", ".join(FAIL))
    raise SystemExit(1 if FAIL else 0)
