#!/usr/bin/env python3
"""Sync Kirishima Mio Design Archive to a structured local mirror and Google Drive."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_SOURCE_URL = "https://sleepyzone.online/mio-design-archive/"
DEFAULT_ROOT_NAME = "Mio-Design-Archive"
DEFAULT_PREFIX = "masterpiece, best quality, very aesthetic, absurdres"
DEFAULT_NEG = (
    "lowres, worst quality, low quality, bad anatomy, bad hands, extra fingers, "
    "missing fingers, text, watermark, signature, jpeg artifacts, multiple views"
)

SERIES_META = {
    "hair": ("01_发型_hair", "发型系列 / Hairstyle Designs"),
    "cloth": ("02_服装_cloth", "服装系列 / Cloth Designs"),
    "pose": ("03_姿势_pose", "姿势系列 / Pose Designs"),
}
SERIES_ORDER = ["hair", "cloth", "pose"]

MIME_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".md": "text/markdown",
    ".json": "application/json",
    ".txt": "text/plain",
}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
FOLDER_MIME = "application/vnd.google-apps.folder"
USER_AGENT = "Mozilla/5.0 (compatible; MioDesignArchiveSync/1.0)"


def sanitize_name(text: str, max_len: int = 80) -> str:
    """Convert arbitrary titles or IDs into safe cross-platform path names."""
    s = str(text or "").strip()
    s = re.sub(r"\s*·\s*", "-", s)
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', "-", s)
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"-{2,}", "-", s)
    s = s.strip(" ._-")
    if len(s) > max_len:
        s = s[:max_len].rstrip(" ._-")
    return s or "untitled"


def volume_folder_name(vol: dict) -> str:
    return f"Vol.{int(vol['vol']):02d}_{sanitize_name(vol.get('theme_zh') or vol.get('theme_en') or vol['slug'])}"


def sheet_filename(vol: dict) -> str:
    return f"00_sheet_{sanitize_name(vol['slug'])}.jpg"


def item_filename(item: dict) -> str:
    return f"{int(item['no']):02d}_{sanitize_name(item['id'])}_{sanitize_name(item['name'])}.jpg"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


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
        key=lambda v: (SERIES_ORDER.index(v["series"]) if v.get("series") in SERIES_ORDER else 99, int(v.get("vol", 0))),
    )
    return {
        "base_url": base,
        "generated": lib.get("generated", ""),
        "source": lib.get("source", ""),
        "volumes": volumes,
        "tagdict": tagdict,
        "prefix": prefix,
        "negative": negative,
    }


def is_valid_jpeg(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            head = f.read(2)
        return path.stat().st_size > 0 and head == b"\xff\xd8"
    except OSError:
        return False


def download_image(url: str, dest: Path, force: bool = False) -> tuple[str, int]:
    """Download JPEG image atomically with magic-byte validation. Returns (status, size)."""
    if not force and is_valid_jpeg(dest):
        return "skipped", dest.stat().st_size
    data = fetch_url_bytes(url, timeout=30, retries=4)
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        raise ValueError(f"Invalid JPEG payload from {url} ({len(data)} bytes)")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, dest)
    return "downloaded", len(data)


def render_volume_readme(vol: dict, prefix: str, negative: str, tagdict: dict) -> str:
    series_key = vol["series"]
    series_label = SERIES_META.get(series_key, (series_key, series_key))[1]
    sheet_fn = sheet_filename(vol)
    lines = [
        f"# {vol['title']}",
        "",
        f"- **系列**：{series_label} (`{series_key}`)",
        f"- **卷号**：Vol.{int(vol['vol']):02d} (`{vol['slug']}`)",
        f"- **主题**：{vol.get('theme_zh', '')} / {vol.get('theme_en', '')}",
        f"- **发布日期**：{vol.get('date', '')}",
        f"- **单品数量**：{vol.get('count', len(vol.get('items', [])))}",
        f"- **卷标签**：{', '.join(vol.get('tags', [])) or '—'}",
        f"- **原帖链接**：{vol.get('url', '')}",
        f"- **九宫格总览图**：`{sheet_fn}`",
        "",
    ]
    if vol.get("note"):
        lines.extend([f"> {vol['note']}", ""])

    lines.extend([
        "## 通用推荐前缀与负向提示词",
        "",
        f"- **推荐正向前缀（可选）**：`{prefix}`",
        f"- **写实模型可改用**：`photorealistic, 35mm photo`",
        f"- **通用负向提示词**：`{negative}`",
        "",
        "---",
        "",
        "## 单件清单与 Prompt",
        "",
    ])

    for item in vol.get("items", []):
        img_fn = item_filename(item)
        prompt = (item.get("prompt") or "").strip()
        prompt_zh = (item.get("prompt_zh") or "").strip()
        if not prompt_zh and prompt:
            toks = [t.strip() for t in prompt.split(",") if t.strip()]
            prompt_zh = "，".join(tagdict.get(t.lower(), t) for t in toks)
        tags_str = ", ".join(item.get("tags") or []) or "—"
        lines.extend([
            f"### {int(item['no']):02d}. {item['name']} (`{item['id']}`)",
            "",
            f"- **图片文件**：`{img_fn}`",
            f"- **标签**：{tags_str}",
            f"- **中文说明**：{item.get('zh') or '—'}",
            "- **English Prompt**：",
            "```text",
            prompt,
            "```",
            "- **中文对照**：",
            "```text",
            prompt_zh,
            "```",
            "",
        ])

    return "\n".join(lines).rstrip() + "\n"


def render_root_readme(site_data: dict) -> str:
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
        f"- **写实模型替换参考**：`photorealistic, 35mm photo`",
        f"- **通用负向提示词**：`{site_data['negative']}`",
        "",
        "## 目录索引",
        "",
    ]

    for series_key in SERIES_ORDER:
        folder_name, label = SERIES_META[series_key]
        s_vols = [v for v in volumes if v.get("series") == series_key]
        s_items = sum(len(v.get("items", [])) for v in s_vols)
        lines.extend([
            f"### {label}（{len(s_vols)} 卷 · {s_items} 件）",
            "",
            "| 卷号 | 目录 | 主题 | 件数 | 日期 | 原帖 |",
            "| --- | --- | --- | ---: | --- | --- |",
        ])
        for v in s_vols:
            v_folder = volume_folder_name(v)
            rel_path = f"{folder_name}/{v_folder}/README.md"
            theme = f"{v.get('theme_zh', '')} ({v.get('theme_en', '')})"
            url = v.get("url", "")
            link = f"[原帖]({url})" if url else "—"
            lines.append(
                f"| Vol.{int(v['vol']):02d} | [`{v_folder}`]({rel_path}) | {theme} | {len(v.get('items', []))} | {v.get('date', '')} | {link} |"
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


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


def write_manifest_if_changed(path: Path, site_data: dict) -> bool:
    """Write manifest.json deterministically; only bump synced_at when catalog content changes."""
    payload = build_manifest_payload(site_data, synced_at="")

    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            existing_cmp = dict(existing)
            existing_cmp["synced_at"] = ""
            if existing_cmp == payload:
                return False
        except (OSError, json.JSONDecodeError):
            pass

    payload["synced_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    return write_text_if_changed(path, text)


def diff_manifest_volumes(site_data: dict, remote_manifest: dict) -> tuple[bool, set[str] | None]:
    """Compare site_data with remote manifest.json; return (changed, only_slugs_or_None)."""
    local_cmp = build_manifest_payload(site_data, synced_at="")
    remote_cmp = dict(remote_manifest)
    remote_cmp["synced_at"] = ""
    if local_cmp == remote_cmp:
        return False, set()

    if (
        local_cmp.get("prefix") != remote_cmp.get("prefix")
        or local_cmp.get("negative") != remote_cmp.get("negative")
        or local_cmp.get("tagdict") != remote_cmp.get("tagdict")
    ):
        return True, None

    remote_vols = {
        v.get("slug"): v
        for v in remote_cmp.get("volumes", [])
        if isinstance(v, dict) and v.get("slug")
    }
    changed_slugs = {
        v["slug"]
        for v in site_data["volumes"]
        if remote_vols.get(v["slug"]) != v
    }
    return True, changed_slugs


def build_local_mirror(
    site_data: dict,
    mirror_dir: Path,
    series_filter: str = "all",
    with_txt: bool = False,
    download_workers: int = 10,
    force_update: bool = False,
    only_slugs: set[str] | None = None,
) -> dict:
    """Download images and render README/manifest files into mirror_dir."""
    mirror_dir.mkdir(parents=True, exist_ok=True)
    all_volumes = site_data["volumes"]
    target_volumes = [
        v
        for v in all_volumes
        if (series_filter == "all" or v.get("series") == series_filter)
        and (only_slugs is None or v.get("slug") in only_slugs)
    ]
    base_url = site_data["base_url"]

    download_tasks: list[tuple[str, Path]] = []
    for vol in target_volumes:
        series_folder = SERIES_META[vol["series"]][0]
        vol_dir = mirror_dir / series_folder / volume_folder_name(vol)
        vol_dir.mkdir(parents=True, exist_ok=True)

        sheet_url = urllib.parse.urljoin(base_url, vol["sheet"])
        download_tasks.append((sheet_url, vol_dir / sheet_filename(vol)))

        for item in vol.get("items", []):
            tile_url = urllib.parse.urljoin(base_url, item["tile"])
            download_tasks.append((tile_url, vol_dir / item_filename(item)))

    downloaded = 0
    skipped = 0
    total_bytes = 0
    errors: list[str] = []

    print(
        f"[mirror] Preparing {len(target_volumes)} volumes ({len(download_tasks)} images) "
        f"into {mirror_dir} ..."
    )
    if download_tasks:
        with ThreadPoolExecutor(max_workers=download_workers) as pool:
            future_map = {
                pool.submit(download_image, url, dest, force_update): (url, dest)
                for url, dest in download_tasks
            }
            for idx, fut in enumerate(as_completed(future_map), 1):
                url, dest = future_map[fut]
                try:
                    status, size = fut.result()
                    total_bytes += size
                    if status == "downloaded":
                        downloaded += 1
                    else:
                        skipped += 1
                except Exception as exc:
                    errors.append(f"{url} -> {dest}: {exc}")
                if idx % 100 == 0 or idx == len(download_tasks):
                    print(
                        f"[mirror] Images progress: {idx}/{len(download_tasks)} "
                        f"(downloaded={downloaded}, skipped={skipped}, errors={len(errors)})"
                    )

    if errors:
        raise RuntimeError("Image download failures:\n  " + "\n  ".join(errors[:20]))

    md_written = 0
    txt_written = 0
    if write_text_if_changed(mirror_dir / "README.md", render_root_readme(site_data)):
        md_written += 1
    write_manifest_if_changed(mirror_dir / "manifest.json", site_data)

    for vol in target_volumes:
        series_folder = SERIES_META[vol["series"]][0]
        vol_dir = mirror_dir / series_folder / volume_folder_name(vol)
        md_content = render_volume_readme(
            vol, site_data["prefix"], site_data["negative"], site_data["tagdict"]
        )
        if write_text_if_changed(vol_dir / "README.md", md_content):
            md_written += 1
        if with_txt:
            for item in vol.get("items", []):
                txt_path = vol_dir / Path(item_filename(item)).with_suffix(".txt")
                prompt_text = (item.get("prompt") or "").strip() + "\n"
                if write_text_if_changed(txt_path, prompt_text):
                    txt_written += 1

    print(
        f"[mirror] Local mirror ready: {len(download_tasks)} images "
        f"({total_bytes / (1024 * 1024):.2f} MB), md_written={md_written}, txt_written={txt_written}"
    )
    return {
        "volumes": len(target_volumes),
        "images": len(download_tasks),
        "downloaded": downloaded,
        "skipped": skipped,
        "total_bytes": total_bytes,
    }


def extract_id_from_output(stdout: str) -> str:
    text = stdout.strip()
    if text:
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                if isinstance(data.get("id"), str) and data["id"]:
                    return data["id"]
                if isinstance(data.get("file"), dict) and data["file"].get("id"):
                    return data["file"]["id"]
            elif isinstance(data, list) and data and isinstance(data[0], dict) and data[0].get("id"):
                return data[0]["id"]
        except json.JSONDecodeError:
            pass
    m = re.search(r'"id"\s*:\s*"([A-Za-z0-9_-]{10,})"', text)
    if m:
        return m.group(1)
    m = re.search(r"\b([A-Za-z0-9_-]{20,})\b", text)
    if m:
        return m.group(1)
    raise RuntimeError(f"Could not extract Drive ID from output: {text!r}")


class GDriveBackend:
    """Incremental Google Drive sync backend powered by the gdrive CLI."""

    def __init__(
        self,
        root_name: str,
        parent_id: str | None,
        state_file: Path,
        upload_workers: int = 5,
        force_update: bool = False,
        dry_run: bool = False,
    ) -> None:
        self.root_name = root_name
        self.parent_id = parent_id
        self.state_file = state_file
        self.upload_workers = upload_workers
        self.force_update = force_update
        self.dry_run = dry_run
        self._state_lock = threading.Lock()
        self.state = self._load_state()

    def _load_state(self) -> dict:
        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except (OSError, json.JSONDecodeError):
                pass
        return {"roots": {}}

    def _save_state(self) -> None:
        if self.dry_run:
            return
        with self._state_lock:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, self.state_file)

    def run_gdrive(self, args: list[str], retries: int = 5) -> str:
        cmd = ["gdrive", *args, "--json"]
        last_err = ""
        for attempt in range(retries):
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode == 0:
                return proc.stdout
            err_msg = (proc.stderr or proc.stdout or "").strip()
            last_err = err_msg
            lower = err_msg.lower()
            if "needs authentication" in lower or "no credentials found" in lower:
                raise RuntimeError(f"gdrive authentication error: {err_msg}")
            if "403" in lower and ("scope" in lower or "permission" in lower) and "rate" not in lower:
                raise RuntimeError(f"gdrive permission error: {err_msg}")
            if attempt + 1 < retries:
                delay = (2**attempt) + random.uniform(0.1, 0.6)
                time.sleep(delay)
        raise RuntimeError(f"Command failed after {retries} attempts ({' '.join(cmd)}): {last_err}")

    def list_children(self, folder_id: str | None = None) -> list[dict]:
        args = ["readonly", "ls"]
        if folder_id:
            args.append(folder_id)
        args.extend(["--max", "0"])
        out = self.run_gdrive(args).strip()
        if not out or out == "null":
            return []
        parsed = json.loads(out)
        return parsed if isinstance(parsed, list) else []

    @staticmethod
    def index_children(items: list[dict]) -> tuple[dict[str, dict], dict[str, dict]]:
        folders: dict[str, dict] = {}
        files: dict[str, dict] = {}
        for item in items:
            name = item.get("name", "")
            if not name:
                continue
            if item.get("mimeType") == FOLDER_MIME:
                folders.setdefault(name, item)
            else:
                files.setdefault(name, item)
        return folders, files

    def mkdir(self, name: str, parent_id: str | None = None) -> str:
        args = ["mutate", "mkdir", name]
        if parent_id:
            args.extend(["--parent", parent_id])
        out = self.run_gdrive(args)
        return extract_id_from_output(out)

    def upload_file(self, local_path: Path, parent_id: str) -> str:
        mime = MIME_TYPES.get(local_path.suffix.lower(), "application/octet-stream")
        out = self.run_gdrive(
            ["mutate", "upload", str(local_path), "--parent", parent_id, "--mime-type", mime]
        )
        return extract_id_from_output(out)

    def update_file(self, file_id: str, local_path: Path) -> None:
        mime = MIME_TYPES.get(local_path.suffix.lower(), "application/octet-stream")
        self.run_gdrive(["mutate", "update", file_id, str(local_path), "--mime-type", mime])

    def _collect_local_tree(self, mirror_dir: Path, series_filter: str) -> tuple[list[Path], dict[str, list[Path]], list[Path]]:
        root_files = sorted(
            [p for p in mirror_dir.iterdir() if p.is_file() and not p.name.endswith(".tmp")],
            key=lambda p: p.name,
        )
        selected_series = [SERIES_META[s][0] for s in SERIES_ORDER if series_filter in ("all", s)]
        series_dirs = [mirror_dir / name for name in selected_series if (mirror_dir / name).is_dir()]
        vol_dirs_by_series: dict[str, list[Path]] = {}
        for sdir in series_dirs:
            vol_dirs_by_series[sdir.name] = sorted(
                [p for p in sdir.iterdir() if p.is_dir()],
                key=lambda p: p.name,
            )
        return root_files, vol_dirs_by_series, series_dirs

    def sync(self, mirror_dir: Path, series_filter: str = "all") -> dict:
        root_files, vol_dirs_by_series, series_dirs = self._collect_local_tree(mirror_dir, series_filter)
        total_local_files = len(root_files) + sum(
            len([f for f in vdir.iterdir() if f.is_file() and not f.name.endswith(".tmp")])
            for vdirs in vol_dirs_by_series.values()
            for vdir in vdirs
        )

        if self.dry_run:
            total_vols = sum(len(v) for v in vol_dirs_by_series.values())
            print(
                f"[dry-run] Would sync '{self.root_name}' (parent={self.parent_id or 'ROOT'}): "
                f"{len(series_dirs)} series folders, {total_vols} volume folders, {total_local_files} files."
            )
            return {"dry_run": True, "files": total_local_files, "folders": 1 + len(series_dirs) + total_vols}

        parent_items = self.list_children(self.parent_id)
        matching_roots = [
            item
            for item in parent_items
            if item.get("name") == self.root_name and item.get("mimeType") == FOLDER_MIME
        ]
        folders_created = 0
        if matching_roots:
            roots_state = self.state.get("roots", {})
            if len(matching_roots) > 1:
                ids_str = ", ".join(item["id"] for item in matching_roots)
                print(
                    f"[gdrive] Warning: found {len(matching_roots)} folders named '{self.root_name}': {ids_str}"
                )
                chosen = max(
                    matching_roots,
                    key=lambda item: len(roots_state.get(item["id"], {})),
                )
            else:
                chosen = matching_roots[0]
            root_id = chosen["id"]
            print(f"[gdrive] Found existing root folder '{self.root_name}' ({root_id})")
        else:
            root_id = self.mkdir(self.root_name, self.parent_id)
            folders_created += 1
            print(f"[gdrive] Created root folder '{self.root_name}' ({root_id})")

        root_state = self.state.setdefault("roots", {}).setdefault(root_id, {})

        root_children = self.list_children(root_id)
        existing_series_folders, existing_root_files = self.index_children(root_children)

        series_folder_ids: dict[str, str] = {}
        for sdir in series_dirs:
            if sdir.name in existing_series_folders:
                series_folder_ids[sdir.name] = existing_series_folders[sdir.name]["id"]
            else:
                sid = self.mkdir(sdir.name, root_id)
                series_folder_ids[sdir.name] = sid
                folders_created += 1
                print(f"[gdrive] Created series folder '{sdir.name}' ({sid})")

        # Ensure volume folders exist per series
        vol_folder_ids: dict[tuple[str, str], str] = {}
        for sname, vdirs in vol_dirs_by_series.items():
            sid = series_folder_ids[sname]
            existing_vols, _ = self.index_children(self.list_children(sid))
            for vdir in vdirs:
                if vdir.name in existing_vols:
                    vol_folder_ids[(sname, vdir.name)] = existing_vols[vdir.name]["id"]
                else:
                    vid = self.mkdir(vdir.name, sid)
                    vol_folder_ids[(sname, vdir.name)] = vid
                    folders_created += 1

        print(f"[gdrive] Folders verified (created={folders_created}). Listing existing volume files...")

        # Fetch existing file listings in all target volume folders concurrently
        existing_vol_files: dict[tuple[str, str], dict[str, dict]] = {}
        with ThreadPoolExecutor(max_workers=max(4, self.upload_workers)) as pool:
            fmap = {
                pool.submit(self.list_children, vid): key
                for key, vid in vol_folder_ids.items()
            }
            for fut in as_completed(fmap):
                key = fmap[fut]
                _, files_map = self.index_children(fut.result())
                existing_vol_files[key] = files_map

        # Build file sync plan: (action, local_path, rel_path, parent_id, existing_file_id, file_hash)
        tasks: list[tuple[str, Path, str, str, str | None, str | None]] = []
        skipped = 0

        def plan_file(local_path: Path, rel_path: str, parent_folder_id: str, remote_files: dict[str, dict]) -> None:
            nonlocal skipped
            existing = remote_files.get(local_path.name)
            is_img = local_path.suffix.lower() in IMAGE_EXTS
            if existing is None:
                fhash = None if is_img else sha256_file(local_path)
                tasks.append(("upload", local_path, rel_path, parent_folder_id, None, fhash))
                return

            remote_id = existing["id"]
            if is_img and not self.force_update:
                with self._state_lock:
                    if rel_path not in root_state:
                        root_state[rel_path] = {
                            "file_id": remote_id,
                            "size": local_path.stat().st_size,
                        }
                skipped += 1
                return

            fhash = sha256_file(local_path)
            cached = root_state.get(rel_path)
            if (
                not self.force_update
                and cached
                and cached.get("file_id") == remote_id
                and cached.get("sha256") == fhash
            ):
                skipped += 1
                return

            tasks.append(("update", local_path, rel_path, parent_folder_id, remote_id, fhash))

        for rf in root_files:
            plan_file(rf, rf.name, root_id, existing_root_files)

        for sname, vdirs in vol_dirs_by_series.items():
            for vdir in vdirs:
                vid = vol_folder_ids[(sname, vdir.name)]
                rfiles = existing_vol_files[(sname, vdir.name)]
                for fp in sorted(
                    [f for f in vdir.iterdir() if f.is_file() and not f.name.endswith(".tmp")],
                    key=lambda p: p.name,
                ):
                    rel = f"{sname}/{vdir.name}/{fp.name}"
                    plan_file(fp, rel, vid, rfiles)

        self._save_state()
        print(
            f"[gdrive] File plan: total={total_local_files}, to_sync={len(tasks)}, skipped={skipped}"
        )

        uploaded = 0
        updated = 0
        errors: list[str] = []

        def run_one(task: tuple[str, Path, str, str, str | None, str | None]) -> str:
            action, local_path, rel_path, parent_folder_id, remote_id, fhash = task
            if action == "upload":
                new_id = self.upload_file(local_path, parent_folder_id)
                entry = {"file_id": new_id, "size": local_path.stat().st_size}
                if fhash:
                    entry["sha256"] = fhash
                with self._state_lock:
                    root_state[rel_path] = entry
                return "uploaded"
            assert remote_id is not None
            self.update_file(remote_id, local_path)
            entry = {"file_id": remote_id, "size": local_path.stat().st_size}
            if fhash:
                entry["sha256"] = fhash
            with self._state_lock:
                root_state[rel_path] = entry
            return "updated"

        if tasks:
            with ThreadPoolExecutor(max_workers=self.upload_workers) as pool:
                fmap = {pool.submit(run_one, t): t for t in tasks}
                for idx, fut in enumerate(as_completed(fmap), 1):
                    task = fmap[fut]
                    try:
                        res = fut.result()
                        if res == "uploaded":
                            uploaded += 1
                        else:
                            updated += 1
                    except Exception as exc:
                        errors.append(f"{task[2]}: {exc}")
                    if idx % 20 == 0 or idx == len(tasks):
                        self._save_state()
                        print(
                            f"[gdrive] Sync progress: {idx}/{len(tasks)} "
                            f"(uploaded={uploaded}, updated={updated}, errors={len(errors)})"
                        )

        self._save_state()
        if errors:
            raise RuntimeError("Drive upload failures:\n  " + "\n  ".join(errors[:20]))

        summary = {
            "root_id": root_id,
            "folders_created": folders_created,
            "uploaded": uploaded,
            "updated": updated,
            "skipped": skipped,
            "total_files": total_local_files,
        }
        print(
            f"[gdrive] Done: root_id={root_id}, folders_created={folders_created}, "
            f"uploaded={uploaded}, updated={updated}, skipped={skipped}"
        )
        return summary


def fetch_remote_manifest_rclone(remote_target: str) -> dict | None:
    """Fetch remote manifest.json via rclone cat; return None only when missing/uninitialized."""
    manifest_remote = f"{remote_target.rstrip('/')}/manifest.json"
    proc = subprocess.run(
        ["rclone", "cat", manifest_remote],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode == 0:
        try:
            data = json.loads(proc.stdout)
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None

    err_msg = (proc.stderr or proc.stdout or "").strip()
    err_lower = err_msg.lower()
    missing_markers = ("not found", "doesn't exist", "object not found", "directory not found")
    if any(marker in err_lower for marker in missing_markers):
        return None
    raise RuntimeError(f"rclone failed to read remote manifest.json ({manifest_remote}): {err_msg}")


def append_github_summary(lines: list[str]) -> None:
    """Append a brief run summary to GITHUB_STEP_SUMMARY when running in GitHub Actions."""
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
    if not summary_path:
        return
    try:
        with Path(summary_path).open("a", encoding="utf-8") as f:
            f.write("\n".join(lines).rstrip() + "\n")
    except OSError:
        pass


def sync_rclone(mirror_dir: Path, remote_target: str, dry_run: bool = False) -> None:
    cmd = ["rclone", "copy", str(mirror_dir), remote_target, "--transfers", "8", "--checkers", "8", "-v"]
    if dry_run:
        cmd.append("--dry-run")
    print(f"[rclone] Running: {' '.join(cmd)}")
    proc = subprocess.run(cmd)
    if proc.returncode != 0:
        raise RuntimeError(f"rclone exited with status {proc.returncode}")


def detect_backend(requested: str) -> str:
    if requested != "auto":
        return requested
    if shutil.which("gdrive"):
        return "gdrive"
    if shutil.which("rclone"):
        return "rclone"
    return "local"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync https://sleepyzone.online/mio-design-archive/ to local mirror and Google Drive."
    )
    parser.add_argument("--source-url", default=DEFAULT_SOURCE_URL, help="Archive website base URL")
    parser.add_argument(
        "--from-local",
        type=Path,
        default=None,
        help="Upload from an existing local Mio-Design-Archive mirror directory without scraping the website",
    )
    parser.add_argument(
        "--mirror-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "mirror" / DEFAULT_ROOT_NAME,
        help="Local mirror output directory",
    )
    parser.add_argument(
        "--backend",
        choices=["auto", "gdrive", "rclone", "local"],
        default="auto",
        help="Target sync backend (default: auto)",
    )
    parser.add_argument("--root-name", default=DEFAULT_ROOT_NAME, help="Target root folder name on Google Drive")
    parser.add_argument("--parent-id", default=None, help="Optional Google Drive parent folder ID")
    parser.add_argument(
        "--rclone-remote",
        default=None,
        help="rclone remote destination (required when --backend rclone, e.g. gdrive:Mio-Design-Archive)",
    )
    parser.add_argument(
        "--series",
        choices=["all", "hair", "cloth", "pose"],
        default="all",
        help="Sync all series or only one series (default: all)",
    )
    parser.add_argument(
        "--with-txt",
        action="store_true",
        help="Also generate per-image .txt prompt files alongside each item JPG",
    )
    parser.add_argument("--download-workers", type=int, default=10, help="Concurrent image download threads")
    parser.add_argument("--upload-workers", type=int, default=5, help="Concurrent Drive upload threads")
    parser.add_argument("--force-update", action="store_true", help="Re-download and re-upload existing files")
    parser.add_argument(
        "--no-remote-check",
        action="store_true",
        help="Skip remote manifest.json pre-check when syncing with rclone",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build local mirror and print planned Drive actions without mutating Google Drive",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    backend = detect_backend(args.backend)
    print(f"[init] Backend={backend}, Series={args.series}, DryRun={args.dry_run}")

    if backend == "rclone":
        remote = args.rclone_remote or f"gdrive:{args.root_name}"
        if args.from_local is not None:
            mirror_dir = args.from_local.resolve()
            if not mirror_dir.is_dir():
                print(f"[error] --from-local directory does not exist: {mirror_dir}", file=sys.stderr)
                return 2
            print(f"[mirror] Using existing local directory: {mirror_dir}")
            sync_rclone(mirror_dir, remote, dry_run=args.dry_run)
            append_github_summary([
                "### Mio Design Archive Sync",
                "- Status: `synced` (from local mirror)",
                f"- Dry run: `{args.dry_run}`",
            ])
            return 0

        site_data = fetch_site_data(args.source_url)
        total_vols = len(site_data["volumes"])
        total_items = sum(len(v.get("items", [])) for v in site_data["volumes"])
        only_slugs: set[str] | None = None

        if (
            args.series == "all"
            and not args.force_update
            and not args.no_remote_check
            and shutil.which("rclone")
        ):
            remote_manifest = fetch_remote_manifest_rclone(remote)
            if remote_manifest is not None:
                changed, only_slugs = diff_manifest_volumes(site_data, remote_manifest)
                if not changed:
                    print(
                        f"[rclone] Remote manifest.json is already up-to-date "
                        f"(generated={site_data.get('generated', '')}, "
                        f"{total_vols} vols / {total_items} items). Nothing to sync."
                    )
                    append_github_summary([
                        "### Mio Design Archive Sync",
                        "- Status: `up-to-date` (skipped downloads & uploads)",
                        f"- Source generated: `{site_data.get('generated', '')}`",
                        f"- Catalog size: `{total_vols}` volumes / `{total_items}` items",
                        "- Synced volumes: `0`",
                    ])
                    return 0

        if only_slugs is not None:
            with tempfile.TemporaryDirectory(prefix="mio-rclone-stage-") as stage_tmp:
                stage_dir = Path(stage_tmp) / DEFAULT_ROOT_NAME
                stats = build_local_mirror(
                    site_data=site_data,
                    mirror_dir=stage_dir,
                    series_filter=args.series,
                    with_txt=args.with_txt,
                    download_workers=max(1, args.download_workers),
                    force_update=args.force_update,
                    only_slugs=only_slugs,
                )
                sync_rclone(stage_dir, remote, dry_run=args.dry_run)
        else:
            mirror_dir = args.mirror_dir.resolve()
            stats = build_local_mirror(
                site_data=site_data,
                mirror_dir=mirror_dir,
                series_filter=args.series,
                with_txt=args.with_txt,
                download_workers=max(1, args.download_workers),
                force_update=args.force_update,
            )
            sync_rclone(mirror_dir, remote, dry_run=args.dry_run)

        append_github_summary([
            "### Mio Design Archive Sync",
            f"- Status: `synced` (`dry_run={args.dry_run}`)",
            f"- Source generated: `{site_data.get('generated', '')}`",
            f"- Catalog size: `{total_vols}` volumes / `{total_items}` items",
            f"- Synced volumes: `{stats['volumes']}` (`{stats['images']}` images)",
        ])
        return 0

    if args.from_local is not None:
        mirror_dir = args.from_local.resolve()
        if not mirror_dir.is_dir():
            print(f"[error] --from-local directory does not exist: {mirror_dir}", file=sys.stderr)
            return 2
        print(f"[mirror] Using existing local directory: {mirror_dir}")
    else:
        mirror_dir = args.mirror_dir.resolve()
        site_data = fetch_site_data(args.source_url)
        build_local_mirror(
            site_data=site_data,
            mirror_dir=mirror_dir,
            series_filter=args.series,
            with_txt=args.with_txt,
            download_workers=max(1, args.download_workers),
            force_update=args.force_update,
        )

    if backend == "local":
        print(f"[done] Local-only mode complete: {mirror_dir}")
        return 0

    if backend == "gdrive":
        if not shutil.which("gdrive") and not args.dry_run:
            print("[error] gdrive CLI not found on PATH. Use --backend rclone or --backend local.", file=sys.stderr)
            return 2
        state_file = Path(__file__).resolve().parent / ".sync_state.json"
        gd = GDriveBackend(
            root_name=args.root_name,
            parent_id=args.parent_id,
            state_file=state_file,
            upload_workers=max(1, args.upload_workers),
            force_update=args.force_update,
            dry_run=args.dry_run,
        )
        gd.sync(mirror_dir=mirror_dir, series_filter=args.series)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
