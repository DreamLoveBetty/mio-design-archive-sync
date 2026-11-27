#!/usr/bin/env python3
"""Incrementally sync Kirishima Mio Design Archive directly into GitHub repo archive/."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_SOURCE_URL = "https://sleepyzone.online/mio-design-archive/"
DEFAULT_ARCHIVE_DIR = Path(__file__).resolve().parent / "archive"
DEFAULT_PREFIX = "masterpiece, best quality, very aesthetic, absurdres"
DEFAULT_NEG = (
    "lowres, worst quality, low quality, bad anatomy, bad hands, extra fingers, "
    "missing fingers, text, watermark, signature, jpeg artifacts, multiple views"
)

SERIES_META = {
    "hair": "发型系列 / Hairstyle Designs",
    "cloth": "服装系列 / Cloth Designs",
    "pose": "姿势系列 / Pose Designs",
}
SERIES_ORDER = ["hair", "cloth", "pose"]
REL_ASSET_PATTERN = re.compile(r"^assets/(?:sheets|tiles)/[A-Za-z0-9._-]+\.(?:jpg|jpeg|png|webp)$")
USER_AGENT = "Mozilla/5.0 (compatible; MioDesignArchiveSync/2.0)"


def validate_rel_asset_path(raw_path: str) -> str:
    """Ensure upstream asset paths stay confined under assets/sheets or assets/tiles."""
    clean = str(raw_path or "").replace("\\", "/").lstrip("/")
    if not REL_ASSET_PATTERN.fullmatch(clean):
        raise ValueError(f"Unsafe upstream asset path: {raw_path!r}")
    return clean


def write_text_if_changed(path: Path, content: str) -> bool:
    """Write UTF-8 text atomically only when content changed; return True if written."""
    normalized = content.replace("\r\n", "\n")
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == normalized:
                return False
        except OSError:
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(normalized, encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    return True


def fetch_url_bytes(url: str, timeout: int = 30, retries: int = 4) -> bytes:
    """Download URL bytes with retry and exponential backoff."""
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            last_err = exc
            if attempt + 1 < retries:
                time.sleep((2**attempt) + random.uniform(0.1, 0.5))
    raise RuntimeError(f"Failed to download {url} after {retries} attempts: {last_err}")


def extract_js_object(js_text: str, var_name: str) -> dict:
    pattern = rf"{re.escape(var_name)}\s*=\s*(\{{.*\}})\s*;?\s*$"
    match = re.search(pattern, js_text, re.S)
    if not match:
        raise ValueError(f"Could not find {var_name} JSON object in JS payload")
    return json.loads(match.group(1))


def extract_js_const(html_text: str, name: str, fallback: str) -> str:
    match = re.search(rf"const\s+{re.escape(name)}\s*=\s*(['\"])(.*?)\1\s*;", html_text)
    return match.group(2) if match else fallback


def fetch_site_data(source_url: str) -> dict:
    """Fetch and parse index.html, data.js, and tagdict.js from the archive site."""
    base = source_url.rstrip("/") + "/"
    html_text = fetch_url_bytes(base).decode("utf-8")
    data_js = fetch_url_bytes(urllib.parse.urljoin(base, "data.js")).decode("utf-8")
    tagdict_js = fetch_url_bytes(urllib.parse.urljoin(base, "tagdict.js")).decode("utf-8")

    lib = extract_js_object(data_js, "window.MIO_LIB")
    tagdict = extract_js_object(tagdict_js, "window.MIO_TAGDICT")
    prefix = extract_js_const(html_text, "PREFIX", DEFAULT_PREFIX)
    negative = extract_js_const(html_text, "NEG", DEFAULT_NEG)

    volumes = sorted(
        lib.get("volumes", []),
        key=lambda v: (
            SERIES_ORDER.index(v["series"]) if v.get("series") in SERIES_ORDER else 99,
            int(v.get("vol", 0)),
        ),
    )
    for vol in volumes:
        vol["sheet"] = validate_rel_asset_path(vol.get("sheet", ""))
        for item in vol.get("items", []):
            item["tile"] = validate_rel_asset_path(item.get("tile", ""))

    return {
        "base_url": base,
        "generated": lib.get("generated", ""),
        "source": lib.get("source", ""),
        "volumes": volumes,
        "tagdict": tagdict,
        "prefix": prefix,
        "negative": negative,
    }


def is_valid_image_bytes(data: bytes) -> bool:
    if len(data) < 12:
        return False
    if data[:2] == b"\xff\xd8":
        return True
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    return False


def is_valid_image_file(path: Path) -> bool:
    try:
        if not path.is_file() or path.stat().st_size < 12:
            return False
        with path.open("rb") as f:
            head = f.read(12)
        return is_valid_image_bytes(head)
    except OSError:
        return False


def download_image(url: str, dest: Path, force: bool = False, dry_run: bool = False) -> tuple[str, int]:
    """Download image atomically with magic-byte validation. Returns (status, size)."""
    if not force and is_valid_image_file(dest):
        return "skipped", dest.stat().st_size
    if dry_run:
        return "would_download", 0
    data = fetch_url_bytes(url, timeout=30, retries=4)
    if not is_valid_image_bytes(data):
        raise ValueError(f"Invalid image payload from {url} ({len(data)} bytes)")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, dest)
    return "downloaded", len(data)


def build_manifest_payload(site_data: dict, synced_at: str = "") -> dict:
    """Build deterministic manifest dictionary for comparison or serialization."""
    volumes = site_data["volumes"]
    by_series = {}
    for s in SERIES_ORDER:
        sv = [v for v in volumes if v.get("series") == s]
        by_series[s] = {
            "volumes": len(sv),
            "items": sum(len(v.get("items", [])) for v in sv),
        }
    total_items = sum(len(v.get("items", [])) for v in volumes)
    return {
        "source_url": site_data["base_url"],
        "source": site_data.get("source", ""),
        "generated": site_data.get("generated", ""),
        "synced_at": synced_at,
        "counts": {
            "volumes": len(volumes),
            "items": total_items,
            "images": len(volumes) + total_items,
            "by_series": by_series,
        },
        "prefix": site_data["prefix"],
        "negative": site_data["negative"],
        "volumes": volumes,
        "tagdict": site_data["tagdict"],
    }


def load_existing_manifest(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def manifest_catalog_changed(site_data: dict, existing_manifest: dict | None) -> bool:
    if not existing_manifest:
        return True
    local_cmp = build_manifest_payload(site_data, synced_at="")
    remote_cmp = dict(existing_manifest)
    remote_cmp["synced_at"] = ""
    return local_cmp != remote_cmp


def write_manifest_if_changed(path: Path, site_data: dict) -> bool:
    """Write manifest.json deterministically; only bump synced_at when catalog content changes."""
    existing = load_existing_manifest(path)
    if not manifest_catalog_changed(site_data, existing):
        return False
    payload = build_manifest_payload(
        site_data,
        synced_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    return write_text_if_changed(path, text)


def render_archive_readme(site_data: dict) -> str:
    volumes = site_data["volumes"]
    total_items = sum(len(v.get("items", [])) for v in volumes)
    total_images = len(volumes) + total_items
    lines = [
        "# 霧島澪 · 姿势与服装设计档案（Mio Design Archive）",
        "",
        f"- **源站地址**：{site_data['base_url']}",
        f"- **原始来源**：{site_data.get('source', '')}",
        f"- **源站数据更新时间**：{site_data.get('generated', '')}",
        f"- **总规模**：{len(volumes)} 卷 / {total_items} 件单品 / {total_images} 张图片",
        "",
        "## 通用推荐前缀与负向提示词",
        "",
        f"- **推荐正向前缀（可选）**：`{site_data['prefix']}`",
        "- **写实模型替换参考**：`photorealistic, 35mm photo`",
        f"- **通用负向提示词**：`{site_data['negative']}`",
        "",
        "## 分卷索引",
        "",
    ]

    for series_key in SERIES_ORDER:
        label = SERIES_META[series_key]
        s_vols = [v for v in volumes if v.get("series") == series_key]
        s_items = sum(len(v.get("items", [])) for v in s_vols)
        lines.extend([
            f"### {label}（{len(s_vols)} 卷 · {s_items} 件）",
            "",
            "| 卷号 | Slug | 主题 | 九宫格 | 件数 | 日期 | 原帖 |",
            "| --- | --- | --- | --- | ---: | --- | --- |",
        ])
        for v in s_vols:
            theme = f"{v.get('theme_zh', '')} ({v.get('theme_en', '')})"
            sheet_rel = v["sheet"]
            url = v.get("url", "")
            link = f"[原帖]({url})" if url else "—"
            lines.append(
                f"| Vol.{int(v['vol']):02d} | `{v['slug']}` | {theme} | "
                f"[`{sheet_rel}`]({sheet_rel}) | {len(v.get('items', []))} | {v.get('date', '')} | {link} |"
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def collect_asset_paths(site_data: dict) -> list[str]:
    seen: set[str] = set()
    rel_paths: list[str] = []
    for vol in site_data["volumes"]:
        for rel in [vol["sheet"], *(item["tile"] for item in vol.get("items", []))]:
            if rel not in seen:
                seen.add(rel)
                rel_paths.append(rel)
    return rel_paths


def append_github_summary(lines: list[str]) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    try:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        pass


def sync_archive(
    *,
    source_url: str,
    archive_dir: Path,
    download_workers: int = 10,
    force_update: bool = False,
    dry_run: bool = False,
) -> int:
    site_data = fetch_site_data(source_url)
    manifest_path = archive_dir / "manifest.json"
    readme_path = archive_dir / "README.md"
    existing_manifest = load_existing_manifest(manifest_path)
    catalog_changed = manifest_catalog_changed(site_data, existing_manifest)

    rel_paths = collect_asset_paths(site_data)
    missing_rel_paths = [
        rel for rel in rel_paths if force_update or not is_valid_image_file(archive_dir / rel)
    ]

    readme_text = render_archive_readme(site_data)
    readme_changed = not readme_path.is_file() or (
        readme_path.read_text(encoding="utf-8") != readme_text.replace("\r\n", "\n")
    )

    total_vols = len(site_data["volumes"])
    total_items = sum(len(v.get("items", [])) for v in site_data["volumes"])

    if not catalog_changed and not missing_rel_paths and not readme_changed:
        print(
            f"[sync] Archive is already up-to-date "
            f"(generated={site_data.get('generated', '')}, "
            f"{total_vols} vols / {total_items} items / {len(rel_paths)} images)."
        )
        append_github_summary([
            "### Mio Design Archive Sync",
            "- Status: `up-to-date`",
            f"- Source generated: `{site_data.get('generated', '')}`",
            f"- Catalog size: `{total_vols}` volumes / `{total_items}` items / `{len(rel_paths)}` images",
        ])
        return 0

    base_url = site_data["base_url"]
    downloaded = 0
    skipped = len(rel_paths) - len(missing_rel_paths)
    errors: list[str] = []

    print(
        f"[sync] Downloading {len(missing_rel_paths)} missing/updated images "
        f"(skipping {skipped} valid files) into {archive_dir} ..."
    )
    if missing_rel_paths:
        with ThreadPoolExecutor(max_workers=max(1, download_workers)) as pool:
            future_map = {
                pool.submit(
                    download_image,
                    urllib.parse.urljoin(base_url, rel),
                    archive_dir / rel,
                    force_update,
                    dry_run,
                ): rel
                for rel in missing_rel_paths
            }
            for idx, fut in enumerate(as_completed(future_map), 1):
                rel = future_map[fut]
                try:
                    status, _ = fut.result()
                    if status in {"downloaded", "would_download"}:
                        downloaded += 1
                    else:
                        skipped += 1
                except Exception as exc:
                    errors.append(f"{rel}: {exc}")
                if idx % 100 == 0 or idx == len(missing_rel_paths):
                    print(
                        f"[sync] Image progress: {idx}/{len(missing_rel_paths)} "
                        f"(downloaded={downloaded}, errors={len(errors)})"
                    )

    if errors:
        raise RuntimeError("Image download failures:\n  " + "\n  ".join(errors[:20]))

    if not dry_run:
        write_manifest_if_changed(manifest_path, site_data)
        write_text_if_changed(readme_path, readme_text)

    print(
        f"[done] Sync complete: {total_vols} vols / {total_items} items, "
        f"downloaded={downloaded}, skipped={skipped}."
    )
    append_github_summary([
        "### Mio Design Archive Sync",
        f"- Status: `{'dry-run' if dry_run else 'synced'}`",
        f"- Source generated: `{site_data.get('generated', '')}`",
        f"- Catalog size: `{total_vols}` volumes / `{total_items}` items / `{len(rel_paths)}` images",
        f"- Downloaded images: `{downloaded}` (skipped `{skipped}`)",
    ])
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync Mio Design Archive directly into GitHub archive/.")
    parser.add_argument("--source-url", default=DEFAULT_SOURCE_URL)
    parser.add_argument("--archive-dir", type=Path, default=DEFAULT_ARCHIVE_DIR)
    parser.add_argument("--download-workers", type=int, default=10)
    parser.add_argument("--force-update", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return sync_archive(
        source_url=args.source_url,
        archive_dir=args.archive_dir.resolve(),
        download_workers=args.download_workers,
        force_update=args.force_update,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    raise SystemExit(main())
