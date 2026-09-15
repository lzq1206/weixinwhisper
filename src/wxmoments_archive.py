from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import wxmoments as core


SCHEMA_VERSION = 1
DEFAULT_TARGET_MIB = 45
DEFAULT_MAX_MIB = 50
VOLUME_PREFIX = "微信朋友圈正式备份_第"
VOLUME_SUFFIX = "卷.pdf"


@dataclass(frozen=True)
class ArchiveItem:
    key: str
    time_text: str
    exported: core.ExportedPost


class ArchiveLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = None

    def __enter__(self) -> "ArchiveLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(self.fd, str(os.getpid()).encode("ascii"))
        except FileExistsError as exc:
            raise RuntimeError(f"另一个归档任务可能正在运行：{self.path}") from exc
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.fd is not None:
            os.close(self.fd)
        with contextlib.suppress(OSError):
            self.path.unlink()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run.bat archive",
        description="从上次断点继续归档自己的朋友圈，并在动态边界自动分卷。",
    )
    parser.add_argument("--until", default="latest", help="截止日期/时间，或 latest（默认）")
    parser.add_argument("--from", dest="from_time", default="", help="首次归档的起始日期/时间；默认从缓存最早记录开始")
    parser.add_argument("--keep-interactions", choices=("y", "n"), default="y", help="保留点赞评论，默认 y")
    parser.add_argument("--allow-download", action="store_true", help="允许联网补充仍可访问的媒体；默认只用本地缓存")
    parser.add_argument("--target-volume-mib", type=int, default=DEFAULT_TARGET_MIB, help="达到此大小后优先新开一卷")
    parser.add_argument("--max-volume-mib", type=int, default=DEFAULT_MAX_MIB, help="单卷硬上限；超大单条动态除外")
    parser.add_argument("--output-root", default="", help="归档 PDF 目录，默认 output/archive")
    parser.add_argument("--state", default="", help="断点状态文件，默认 runtime/archive_state.json")
    parser.add_argument("--config", default=str(core.DEFAULT_CONFIG), help=argparse.SUPPRESS)
    parser.add_argument("--key", default="", help=argparse.SUPPRESS)
    parser.add_argument("--dry-run", action="store_true", help="只扫描将新增多少条，不写 PDF 和状态")
    parser.add_argument("--quiet", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def mib_bytes(value: int) -> int:
    if value <= 0:
        raise ValueError("分卷大小必须大于 0 MiB")
    return value * 1024 * 1024


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp_path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp_path, path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def archive_post_key(post: dict[str, Any]) -> str:
    raw = json.dumps(core.timeline_post_dedupe_key(post), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def keys_digest(keys: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for key in keys:
        digest.update(key.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def volume_filename(number: int) -> str:
    return f"{VOLUME_PREFIX}{number:03d}{VOLUME_SUFFIX}"


def new_state(account: str, output_root: Path, target_bytes: int, max_bytes: int) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "account": account,
        "output_root": str(output_root.resolve()),
        "order": "oldest",
        "dedupe": "sha256(timeline_post_dedupe_key)",
        "target_volume_bytes": target_bytes,
        "max_volume_bytes": max_bytes,
        "archive_start_time": "",
        "last_exported_time": "",
        "post_count": 0,
        "exported_post_keys": [],
        "volumes": [],
        "pending": None,
        "updated_at": "",
    }


def load_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise RuntimeError(f"断点状态无法读取：{path}: {exc}") from exc
    if not isinstance(state, dict) or state.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError(f"不支持的归档状态格式：{path}")
    if not isinstance(state.get("volumes"), list) or not isinstance(state.get("exported_post_keys"), list):
        raise RuntimeError(f"归档状态字段损坏：{path}")
    return state


def validate_state(state: dict[str, Any], account: str, output_root: Path) -> None:
    if str(state.get("account") or "").lower() != account.lower():
        raise RuntimeError("断点状态属于另一个微信账号，请使用不同的 --state 和 --output-root")
    expected = os.path.normcase(str(output_root.resolve()))
    actual = os.path.normcase(str(Path(str(state.get("output_root") or "")).resolve()))
    if expected != actual:
        raise RuntimeError(f"断点状态绑定的输出目录是 {state.get('output_root')}，不能改到 {output_root}")


def apply_pending(state: dict[str, Any]) -> None:
    pending = state.get("pending")
    if not isinstance(pending, dict):
        return
    record = pending.get("volume")
    if not isinstance(record, dict):
        raise RuntimeError("未完成事务缺少卷信息")
    number = int(record.get("number") or 0)
    volumes = [item for item in state["volumes"] if int(item.get("number") or 0) != number]
    volumes.append(record)
    volumes.sort(key=lambda item: int(item.get("number") or 0))
    state["volumes"] = volumes
    existing = set(str(item) for item in state["exported_post_keys"])
    existing.update(str(item) for item in pending.get("added_keys") or [])
    state["exported_post_keys"] = sorted(existing)
    state["post_count"] = len(existing)
    all_times = [str(value) for item in volumes for value in item.get("times") or []]
    state["archive_start_time"] = min(all_times) if all_times else ""
    state["last_exported_time"] = max(all_times) if all_times else ""
    state["pending"] = None
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")


def recover_pending(state: dict[str, Any], state_path: Path, output_root: Path) -> None:
    pending = state.get("pending")
    if not isinstance(pending, dict):
        return
    record = pending.get("volume") if isinstance(pending.get("volume"), dict) else {}
    final_path = output_root / str(record.get("filename") or "")
    new_sha = str(record.get("sha256") or "")
    old_sha = str(pending.get("old_sha256") or "")
    actual_sha = sha256_file(final_path) if final_path.is_file() else ""
    if actual_sha == new_sha:
        apply_pending(state)
        atomic_write_json(state_path, state)
        return
    if actual_sha == old_sha or (not actual_sha and not old_sha):
        state["pending"] = None
        atomic_write_json(state_path, state)
        return
    raise RuntimeError(f"检测到无法自动恢复的未完成归档事务：{final_path}")


def filter_raw_posts(
    posts: list[dict[str, Any]],
    start: datetime | None,
    end: datetime | None,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for post in posts:
        created = core.post_created_datetime(post)
        if created is None or (start and created < start) or (end and created > end):
            continue
        selected.append(post)
    return sorted(selected, key=lambda item: int(item.get("createTime") or 0))


def pair_unexported(
    raw_posts: list[dict[str, Any]],
    exported_posts: list[core.ExportedPost],
    exported_keys: set[str],
) -> list[ArchiveItem]:
    if len(raw_posts) != len(exported_posts):
        raise RuntimeError(f"解析结果不一致：数据库 {len(raw_posts)} 条，排版结果 {len(exported_posts)} 条")
    items: list[ArchiveItem] = []
    seen = set(exported_keys)
    for raw, exported in zip(raw_posts, exported_posts):
        key = archive_post_key(raw)
        if key in seen:
            continue
        seen.add(key)
        items.append(ArchiveItem(key=key, time_text=exported.time_text, exported=exported))
    return items


def _pdf_modules() -> tuple[Any, Any]:
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError as exc:
        raise RuntimeError("缺少 pypdf，请重新运行 run.bat 自动安装依赖") from exc
    return PdfReader, PdfWriter


def build_pdf_candidate(
    content_root: Path,
    items: list[ArchiveItem],
    destination: Path,
    existing_pdf: Path | None = None,
    existing_times: list[str] | None = None,
    existing_keys: list[str] | None = None,
) -> None:
    PdfReader, PdfWriter = _pdf_modules()
    fragment = destination.with_name(f".{destination.name}.fragment.pdf")
    with contextlib.suppress(OSError):
        fragment.unlink()
    core.render_pdf(content_root, [item.exported for item in items], fragment)
    writer = PdfWriter()
    if existing_pdf is not None:
        for page in PdfReader(str(existing_pdf), strict=False).pages:
            writer.add_page(page)
    for page in PdfReader(str(fragment), strict=False).pages:
        writer.add_page(page)
    times = list(existing_times or []) + [item.time_text for item in items]
    keys = list(existing_keys or []) + [item.key for item in items]
    writer.add_metadata(
        {
            "/Title": "微信朋友圈正式备份",
            "/Creator": "wxMoments archive",
            "/WxMomentsPostCount": str(len(keys)),
            "/WxMomentsFirst": times[0] if times else "",
            "/WxMomentsLast": times[-1] if times else "",
            "/WxMomentsPostKeysSha256": keys_digest(keys),
        }
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as handle:
        writer.write(handle)
    with contextlib.suppress(OSError):
        fragment.unlink()


def validate_pdf(path: Path, times: list[str], keys: list[str], *, visual: bool = False) -> dict[str, Any]:
    PdfReader, _ = _pdf_modules()
    if not path.is_file() or path.stat().st_size < 100 or not path.read_bytes()[:5] == b"%PDF-":
        raise RuntimeError(f"PDF 文件无效：{path}")
    try:
        reader = PdfReader(str(path), strict=False)
        page_count = len(reader.pages)
        metadata = reader.metadata or {}
        for page in reader.pages:
            _ = page.mediabox
    except Exception as exc:
        raise RuntimeError(f"PDF 重新打开校验失败：{path}: {exc}") from exc
    if page_count <= 0:
        raise RuntimeError(f"PDF 没有页面：{path}")
    expected = {
        "/WxMomentsPostCount": str(len(keys)),
        "/WxMomentsFirst": times[0] if times else "",
        "/WxMomentsLast": times[-1] if times else "",
        "/WxMomentsPostKeysSha256": keys_digest(keys),
    }
    for name, value in expected.items():
        if str(metadata.get(name) or "") != value:
            raise RuntimeError(f"PDF 清单校验失败：{path.name} {name}")
    visual_pages: list[int] = []
    if visual:
        try:
            import pypdfium2 as pdfium
            from PIL import ImageStat

            document = pdfium.PdfDocument(str(path))
            try:
                visual_pages = sorted({0, len(document) - 1})
                for page_number in visual_pages:
                    page = document[page_number]
                    bitmap = None
                    image = None
                    try:
                        bitmap = page.render(scale=0.6)
                        image = bitmap.to_pil().convert("RGB")
                        stat = ImageStat.Stat(image)
                        if max(stat.var) < 0.05 and min(stat.mean) > 250:
                            raise RuntimeError(f"PDF 第 {page_number + 1} 页疑似空白：{path.name}")
                    finally:
                        if image is not None:
                            image.close()
                        if bitmap is not None:
                            bitmap.close()
                        page.close()
            finally:
                document.close()
        except ImportError:
            visual_pages = []
    return {"pages": page_count, "bytes": path.stat().st_size, "visual_pages": [item + 1 for item in visual_pages]}


def current_volume(state: dict[str, Any]) -> dict[str, Any] | None:
    volumes = state.get("volumes") or []
    return volumes[-1] if volumes else None


def write_manifest(output_root: Path, state: dict[str, Any], report: dict[str, Any]) -> None:
    manifest = {
        key: value
        for key, value in state.items()
        if key not in {"exported_post_keys", "pending"}
    }
    manifest["last_run"] = report
    atomic_write_json(output_root / "archive_manifest.json", manifest)
    atomic_write_json(output_root / "archive_report.json", report)


def commit_volume(
    state: dict[str, Any],
    state_path: Path,
    output_root: Path,
    number: int,
    candidate: Path,
    items: list[ArchiveItem],
    previous: dict[str, Any] | None,
) -> dict[str, Any]:
    final_path = output_root / volume_filename(number)
    previous_times = list(previous.get("times") or []) if previous else []
    previous_keys = list(previous.get("post_keys") or []) if previous else []
    times = previous_times + [item.time_text for item in items]
    keys = previous_keys + [item.key for item in items]
    qa = validate_pdf(candidate, times, keys, visual=True)
    new_sha = sha256_file(candidate)
    record = {
        "number": number,
        "filename": final_path.name,
        "start_time": times[0],
        "last_time": times[-1],
        "post_count": len(keys),
        "pdf_bytes": candidate.stat().st_size,
        "sha256": new_sha,
        "pages": qa["pages"],
        "times": times,
        "post_keys": keys,
        "oversize": candidate.stat().st_size > int(state["max_volume_bytes"]),
    }
    old_sha = sha256_file(final_path) if final_path.is_file() else ""
    state["pending"] = {
        "volume": record,
        "added_keys": [item.key for item in items],
        "old_sha256": old_sha,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    atomic_write_json(state_path, state)
    os.replace(candidate, final_path)
    apply_pending(state)
    atomic_write_json(state_path, state)
    return record


def largest_fitting_prefix(
    content_root: Path,
    items: list[ArchiveItem],
    work_dir: Path,
    max_bytes: int,
    previous: dict[str, Any] | None,
    existing_pdf: Path | None,
) -> tuple[int, Path | None]:
    previous_times = list(previous.get("times") or []) if previous else []
    previous_keys = list(previous.get("post_keys") or []) if previous else []
    cache: dict[int, Path] = {}

    def render(count: int) -> Path:
        if count in cache:
            return cache[count]
        path = work_dir / f"candidate-{count}.pdf"
        build_pdf_candidate(
            content_root,
            items[:count],
            path,
            existing_pdf,
            previous_times,
            previous_keys,
        )
        cache[count] = path
        return path

    full = render(len(items))
    if full.stat().st_size <= max_bytes:
        return len(items), full
    low, high, best = 1, len(items) - 1, 0
    while low <= high:
        middle = (low + high) // 2
        candidate = render(middle)
        if candidate.stat().st_size <= max_bytes:
            best = middle
            low = middle + 1
        else:
            high = middle - 1
    if best == 0 and previous is not None:
        for path in cache.values():
            with contextlib.suppress(OSError):
                path.unlink()
        return 0, None
    if best == 0:
        best = 1
    chosen = render(best)
    for count, path in cache.items():
        if count != best:
            with contextlib.suppress(OSError):
                path.unlink()
    return best, chosen


def parse_until(value: str) -> datetime | None:
    if str(value or "").strip().lower() == "latest":
        return None
    parsed = core.parse_datetime(value, end_of_day=True)
    if parsed is None:
        raise ValueError("--until 必须是日期/时间或 latest")
    return parsed


async def _archive(args: argparse.Namespace) -> int:
    if os.name != "nt":
        raise RuntimeError("archive 正式命令仅支持 Windows")
    target_bytes = mib_bytes(args.target_volume_mib)
    max_bytes = mib_bytes(args.max_volume_mib)
    if target_bytes > max_bytes:
        raise ValueError("--target-volume-mib 不能大于 --max-volume-mib")
    config_path = Path(args.config).expanduser()
    config = core.load_config(config_path)
    core.ensure_imports()
    output_root = Path(args.output_root or core.PROJECT_ROOT / "output" / "archive").expanduser()
    state_path = Path(args.state or core.RUNTIME_DIR / "archive_state.json").expanduser()
    output_root.mkdir(parents=True, exist_ok=True)

    with ArchiveLock(state_path.with_suffix(state_path.suffix + ".lock")):
        try:
            account_info = core.find_account(config)
        except FileNotFoundError as exc:
            if args.quiet:
                raise
            account_info = core.find_account_with_interactive_retry(config, config_path, exc)
        state = load_state(state_path)
        if state is None:
            existing = list(output_root.glob(f"{VOLUME_PREFIX}*{VOLUME_SUFFIX}"))
            if existing:
                raise RuntimeError(
                    f"输出目录已有 {len(existing)} 个归档 PDF，但没有断点状态。请换一个空目录，避免误覆盖。"
                )
            state = new_state(account_info.account, output_root, target_bytes, max_bytes)
        else:
            validate_state(state, account_info.account, output_root)
            recover_pending(state, state_path, output_root)
            target_bytes = int(state["target_volume_bytes"])
            max_bytes = int(state["max_volume_bytes"])

        key = core.acquire_db_key(account_info, config, args)
        if not re.fullmatch(rf"[0-9a-fA-F]{{{core.DB_KEY_HEX_LENGTH}}}", key):
            raise ValueError("数据库密钥必须是 64 位十六进制字符串")
        print("[1/5] 制作并解密本地数据库快照…", flush=True)
        account_dir = core.decrypt_databases(account_info, key)
        core.save_db_key(account_info, account_dir, key)
        usernames = core.self_username_candidates(account_info, config)
        timeline = core.load_timeline(account_dir, usernames, source="decrypted")
        dated = [core.post_created_datetime(post) for post in timeline]
        dated = sorted(item for item in dated if item is not None)
        if not dated:
            raise RuntimeError("本地缓存中没有找到自己的朋友圈记录")
        requested_from = core.parse_datetime(args.from_time, end_of_day=False) if args.from_time else None
        if state["post_count"] and requested_from:
            raise ValueError("已有断点时不能再使用 --from；工具会自动从上次位置继续")
        checkpoint = core.parse_datetime(str(state.get("last_exported_time") or ""), end_of_day=False)
        start = checkpoint or requested_from or dated[0]
        end = parse_until(args.until)
        known_keys = set(str(item) for item in state["exported_post_keys"])
        archive_floor = core.parse_datetime(str(state.get("archive_start_time") or ""), end_of_day=False) or start
        if checkpoint:
            historical_scope = filter_raw_posts(timeline, archive_floor, end)
            late_backfill = [
                post
                for post in historical_scope
                if archive_post_key(post) not in known_keys
                and (core.post_created_datetime(post) or checkpoint) < checkpoint
            ]
            if late_backfill:
                late_times = sorted(core.post_created_datetime(post) for post in late_backfill)
                raise RuntimeError(
                    f"发现 {len(late_backfill)} 条早于当前断点、但尚未归档的历史记录（最早 "
                    f"{late_times[0]:%Y-%m-%d %H:%M:%S}）。为保证严格时间顺序，工具拒绝把它们追加到末尾；"
                    "请用新的空 --output-root 和 --state 从最早日期重建归档。"
                )
        raw_selected = filter_raw_posts(timeline, start, end)
        raw_new_count = sum(archive_post_key(post) not in known_keys for post in raw_selected)
        print(
            f"[2/5] 缓存范围 {dated[0]:%Y-%m-%d %H:%M:%S} ～ {dated[-1]:%Y-%m-%d %H:%M:%S}；"
            f"本次新增 {raw_new_count} 条。",
            flush=True,
        )
        if args.dry_run or raw_new_count == 0:
            if raw_new_count == 0:
                print("没有发现尚未归档的新动态。", flush=True)
            return 0

        print("[3/5] 解析文字、完整比例图片、位置、链接、视频封面和互动…", flush=True)
        await core.save_image_keys(account_info.account, account_info.wxid_dir, account_info.db_storage_dir)
        contacts = core.load_contact_entries(account_dir) if args.keep_interactions == "y" else []
        contact_names = core.build_contact_display_names(contacts)
        core.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="archive-job-", dir=str(core.RUNTIME_DIR)) as temp_name:
            content_root = Path(temp_name) / "content"
            content_root.mkdir(parents=True)
            _, exported = await core.export_markdown(
                account_info,
                account_dir,
                content_root,
                start,
                end,
                usernames,
                config,
                args.keep_interactions == "y",
                contact_names,
                allow_download=bool(args.allow_download),
                order="oldest",
            )
            items = pair_unexported(raw_selected, exported, known_keys)
            if not items:
                print("扫描结果均已存在于归档中。", flush=True)
                return 0
            print(f"[4/5] 按 {target_bytes // 1048576}～{max_bytes // 1048576} MiB 规则写入 PDF…", flush=True)
            work_dir = Path(temp_name) / "pdf"
            work_dir.mkdir()
            remaining = items
            changed: list[dict[str, Any]] = []
            while remaining:
                previous = current_volume(state)
                reuse = bool(previous and int(previous.get("pdf_bytes") or 0) < target_bytes)
                if reuse:
                    number = int(previous["number"])
                    existing_pdf = output_root / str(previous["filename"])
                    if not existing_pdf.is_file() or sha256_file(existing_pdf) != str(previous.get("sha256") or ""):
                        raise RuntimeError(f"现有卷被修改或丢失：{existing_pdf}")
                else:
                    previous = None
                    existing_pdf = None
                    volumes = state.get("volumes") or []
                    number = int(volumes[-1]["number"]) + 1 if volumes else 1
                count, candidate = largest_fitting_prefix(
                    content_root,
                    remaining,
                    work_dir,
                    max_bytes,
                    previous,
                    existing_pdf,
                )
                if count == 0:
                    previous = None
                    existing_pdf = None
                    number += 1
                    count, candidate = largest_fitting_prefix(
                        content_root,
                        remaining,
                        work_dir,
                        max_bytes,
                        previous,
                        existing_pdf,
                    )
                if candidate is None:
                    raise RuntimeError("无法生成新的 PDF 卷")
                record = commit_volume(
                    state,
                    state_path,
                    output_root,
                    number,
                    candidate,
                    remaining[:count],
                    previous,
                )
                changed.append(record)
                remaining = remaining[count:]

        report = {
            "completed_at": datetime.now().isoformat(timespec="seconds"),
            "cached_earliest": dated[0].strftime("%Y-%m-%d %H:%M:%S"),
            "cached_latest": dated[-1].strftime("%Y-%m-%d %H:%M:%S"),
            "requested_until": args.until,
            "added_posts": len(items),
            "total_posts": state["post_count"],
            "last_exported_time": state["last_exported_time"],
            "changed_volumes": [
                {key: value for key, value in item.items() if key not in {"times", "post_keys"}}
                for item in changed
            ],
        }
        write_manifest(output_root, state, report)
        print("[5/5] 已重新打开校验 PDF、更新断点和清单。", flush=True)
        print(
            f"归档完成：新增 {len(items)} 条，累计 {state['post_count']} 条，"
            f"最新 {state['last_exported_time']}\n输出目录：{output_root.resolve()}",
            flush=True,
        )
        return 0


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.quiet:
            core.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
            log_path = core.RUNTIME_DIR / "archive_last_run.log"
            with log_path.open("w", encoding="utf-8") as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                return await _archive(args)
        return await _archive(args)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"\n归档失败：{exc}", file=sys.stderr, flush=True)
        if os.environ.get("WXMOMENTS_DEBUG_TRACEBACK", "").lower() in {"1", "true", "yes", "on"}:
            raise
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
